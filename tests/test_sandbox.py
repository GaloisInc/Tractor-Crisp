import contextlib
import io
import os
import pwd
import signal
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crisp.__main__ import exit_on_signal, install_exit_handlers
from crisp.sandbox import bwrap as sandbox_bwrap
from crisp.sandbox import docker as sandbox_docker
from crisp.sandbox import sudo as sandbox_sudo
from crisp import work_dir


class WorkContainerCleanupTest(unittest.TestCase):
    def test_container_stops_when_the_caller_raises(self):
        with patch.object(sandbox_docker, 'WorkContainer') as wc_class:
            with self.assertRaises(KeyboardInterrupt):
                with sandbox_docker.run_work_container(None, None):
                    raise KeyboardInterrupt
        wc_class.return_value.stop.assert_called_once()

    def test_kept_container_ends_commands(self):
        # With `--keep-work-container`, the agent must not keep running.
        with patch.object(sandbox_docker, 'WorkContainer') as wc_class, \
                patch.object(sandbox_docker, 'KEEP_WORK_CONTAINER', True), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                with sandbox_docker.run_work_container(None, None):
                    raise KeyboardInterrupt
        wc_class.return_value.end_commands.assert_called_once()
        wc_class.return_value.stop.assert_not_called()

    def test_interrupted_stop_warns(self):
        # Another Ctrl-C while the container is being stopped gets through,
        # but says what is left.
        class Interrupted(Exception):
            pass
        out = io.StringIO()
        with patch.object(sandbox_docker, 'WorkContainer') as wc_class, \
                contextlib.redirect_stdout(out):
            wc_class.return_value.container.name = 'crisp-work'
            wc_class.return_value.stop.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                with sandbox_docker.run_work_container(None, None):
                    raise Interrupted
        self.assertIn("exiting without cleaning up container 'crisp-work'", out.getvalue())


class BwrapCleanupTest(unittest.TestCase):
    def test_bwrap_dies_with_crisp(self):
        sb = object.__new__(sandbox_bwrap.BwrapSandbox)
        sb.work_dir = SimpleNamespace(path = '/tmp/work')
        sb.bwrap_cfg = SimpleNamespace(extra_search_path = [], dirs = {})
        with patch.object(sandbox_bwrap.subprocess, 'run',
                    return_value = subprocess.CompletedProcess([], 0, b'')) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            sb.run(['true'])
        bwrap_cmd = run.call_args.args[0]
        self.assertIn('--die-with-parent', bwrap_cmd[:bwrap_cmd.index('--')])

    def test_failed_work_dir_removal_only_warns(self):
        # The agent may still be writing files there; the error being handled
        # must not be replaced.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = SimpleNamespace(mvir_storage_dir = tmp.name)
        out = io.StringIO()
        with patch.object(work_dir.shutil, 'rmtree', side_effect = OSError('not empty')), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(KeyboardInterrupt):
                with work_dir.lock_work_dir(cfg, None, dir_suffix = 'x'):
                    raise KeyboardInterrupt
        self.assertIn('could not remove', out.getvalue())


class SudoSandboxStopTest(unittest.TestCase):
    # Runs commands as the current user, without `sudo`, so that the
    # processes are real.
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        with patch.dict(os.environ, {'TMPDIR': self.tmp}):
            self.sb = sandbox_sudo.SudoSandbox(None, pwd.getpwuid(os.getuid()).pw_name)
        self.sb._sudo_cmd = lambda cmd, env: ('env', *(f'{k}={v}' for k, v in env.items()), *cmd)
        self.sb.start()
        for name, value in (('STOP_TIMEOUT', 1), ('KEEP_TEMP_DIR', False)):
            p = patch.object(sandbox_sudo, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_in_background(self, cmd):
        result = []
        def run():
            with contextlib.redirect_stdout(io.StringIO()):
                result.append(self.sb.run(cmd, stream = True))
        t = threading.Thread(target = run, daemon = True)
        t.start()
        return t, result

    def wait_for_file(self, path):
        deadline = time.monotonic() + 5
        while not os.path.exists(path) or os.path.getsize(path) == 0:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        time.sleep(0.05)
        with open(path) as f:
            return int(f.read())

    def assert_exits(self, pid):
        # Killed processes are reaped by init, so wait until they're gone.
        deadline = time.monotonic() + 5
        while os.path.exists(f'/proc/{pid}'):
            with open(f'/proc/{pid}/stat') as f:
                if f.read().split(') ')[1].startswith('Z'):
                    return
            self.assertLess(time.monotonic(), deadline, f'process {pid} still running')
            time.sleep(0.01)

    def test_stop_removes_the_dir(self):
        self.sb.stop()
        self.assertFalse(os.path.exists(self.sb.dir_path))

    def test_stop_ends_a_running_command(self):
        # E.g. codex, while crisp is being stopped.
        pid_file = os.path.join(self.tmp, 'pid')
        t, result = self.run_in_background(
            ['sh', '-c', f'echo $$ >{pid_file}; exec sleep 60'])
        pid = self.wait_for_file(pid_file)
        t0 = time.monotonic()
        self.sb.stop()
        # SIGTERM ended it, without waiting for `STOP_TIMEOUT`.
        self.assertLess(time.monotonic() - t0, 0.9)
        t.join(5)
        self.assertNotEqual(result[0][0], 0)
        self.assert_exits(pid)
        self.assertFalse(os.path.exists(self.sb.dir_path))

    def test_stop_keeps_the_dir_if_a_command_keeps_running(self):
        # `sudo` can't relay SIGKILL, so a command that ignores SIGTERM keeps
        # running; its directory must not be removed under it.
        self.sb._sudo_cmd = lambda cmd, env: ('sh', '-c', 'trap "" TERM; exec "$@"', 'sh', *cmd)
        pid_file = os.path.join(self.tmp, 'pid')
        t, result = self.run_in_background(
            ['sh', '-c', f'echo $$ >{pid_file}; while :; do sleep 0.1; done'])
        self.wait_for_file(pid_file)
        [p] = self.sb._procs
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.sb.stop()
        self.assertIn('commands are still running', out.getvalue())
        self.assertTrue(os.path.exists(self.sb.dir_path))

    def test_interrupted_stop_warns(self):
        # Another Ctrl-C while commands are being ended gets through, but
        # says what is left.
        pid_file = os.path.join(self.tmp, 'pid')
        t, result = self.run_in_background(
            ['sh', '-c', f'echo $$ >{pid_file}; exec sleep 60'])
        self.wait_for_file(pid_file)
        [p] = self.sb._procs
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        out = io.StringIO()
        with patch.object(self.sb, '_end_commands', side_effect = KeyboardInterrupt), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(KeyboardInterrupt):
                self.sb.stop()
        self.assertIn(f'exiting without cleaning up processes [{p.pid}]', out.getvalue())
        self.assertIn(f'exiting without removing {self.sb.dir_path!r}', out.getvalue())
        self.assertTrue(os.path.exists(self.sb.dir_path))

    def test_failed_removal_only_warns(self):
        sudo_cmd = self.sb._sudo_cmd
        self.sb._sudo_cmd = lambda cmd, env: ('false',) if cmd[0] == 'rm' else sudo_cmd(cmd, env)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.sb.stop()
        self.assertIn('could not remove', out.getvalue())

    def test_no_commands_start_after_stop(self):
        self.sb.stop()
        with self.assertRaises(RuntimeError), contextlib.redirect_stdout(io.StringIO()):
            self.sb.run(['true'])

    def test_run_sudo_checks_and_passes_input(self):
        p = self.sb._run_sudo(('cat',), input = b'data', stdout = subprocess.PIPE)
        self.assertEqual(p.stdout, b'data')
        with self.assertRaises(subprocess.CalledProcessError):
            self.sb._run_sudo(('false',))
        self.assertEqual(self.sb._procs, set())


class ExitHandlersTest(unittest.TestCase):
    def setUp(self):
        # Restore the test process's own handlers afterward.
        for sig in (signal.SIGTERM, signal.SIGHUP):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))

    def test_signals_exit_normally(self):
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.assertRaises(SystemExit) as cm:
                exit_on_signal(sig, None)
            self.assertEqual(cm.exception.code, 128 + sig)

    def test_sigterm_and_sighup_are_handled(self):
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        install_exit_handlers()
        self.assertIs(signal.getsignal(signal.SIGTERM), exit_on_signal)
        self.assertIs(signal.getsignal(signal.SIGHUP), exit_on_signal)

    def test_nohup_keeps_sighup_ignored(self):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        install_exit_handlers()
        self.assertIs(signal.getsignal(signal.SIGTERM), exit_on_signal)
        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)


if __name__ == '__main__':
    unittest.main()
