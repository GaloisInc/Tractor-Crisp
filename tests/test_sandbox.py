import contextlib
import io
import signal
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crisp.__main__ import exit_on_signal, install_exit_handlers
from crisp.sandbox import bwrap as sandbox_bwrap
from crisp.sandbox import docker as sandbox_docker
from crisp.sandbox import sudo as sandbox_sudo


class WorkContainerCleanupTest(unittest.TestCase):
    def test_container_stops_when_the_caller_raises(self):
        with patch.object(sandbox_docker, 'WorkContainer') as wc_class:
            with self.assertRaises(KeyboardInterrupt):
                with sandbox_docker.run_work_container(None, None):
                    raise KeyboardInterrupt
        wc_class.return_value.stop.assert_called_once()

    def test_second_signal_does_not_interrupt_the_stop(self):
        # Not `KeyboardInterrupt`, which would abort the whole test run if it
        # escaped.
        class Interrupted(Exception):
            pass
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        signal.signal(signal.SIGTERM, exit_on_signal)
        stopped = []
        def stop():
            signal.raise_signal(signal.SIGTERM)
            stopped.append(True)
        with patch.object(sandbox_docker, 'WorkContainer') as wc_class:
            wc_class.return_value.stop.side_effect = stop
            # The SIGTERM is raised again once the container is stopped.
            with self.assertRaises(SystemExit):
                with sandbox_docker.run_work_container(None, None):
                    raise Interrupted
        self.assertEqual(stopped, [True])


class DeferredExitSignalsTest(unittest.TestCase):
    def setUp(self):
        for sig in sandbox_docker.EXIT_SIGNALS:
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))

    def test_signals_are_raised_after_the_block(self):
        class Stopped(Exception):
            pass
        def stop(signum, frame):
            raise Stopped
        signal.signal(signal.SIGTERM, stop)
        finished = False
        with self.assertRaises(Stopped):
            with sandbox_docker.deferred_exit_signals():
                signal.raise_signal(signal.SIGTERM)
                finished = True
        self.assertTrue(finished)
        self.assertIs(signal.getsignal(signal.SIGTERM), stop)

    def test_ignored_signals_stay_ignored(self):
        # E.g. SIGHUP under `nohup`.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        with sandbox_docker.deferred_exit_signals():
            self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)

    def test_other_threads_are_left_alone(self):
        # `signal.signal` raises `ValueError` off the main thread.
        errors = []
        def run():
            try:
                with sandbox_docker.deferred_exit_signals():
                    pass
            except BaseException as e:
                errors.append(e)
        t = threading.Thread(target = run)
        t.start()
        t.join()
        self.assertEqual(errors, [])


class CommandsEndOnExitTest(unittest.TestCase):
    def test_sandbox_flags(self):
        # Stopping the container or `--die-with-parent` ends the agent; sudo
        # has neither, so crisp waits for it.
        self.assertTrue(sandbox_docker.WorkContainer.COMMANDS_END_ON_EXIT)
        self.assertTrue(sandbox_bwrap.BwrapSandbox.COMMANDS_END_ON_EXIT)
        self.assertFalse(sandbox_sudo.SudoSandbox.COMMANDS_END_ON_EXIT)

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
