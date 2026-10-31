import os
import signal
import subprocess
import sys
import threading
import time
import unittest

from crisp.http_server import RunnerThread


class RunnerThreadExitTest(unittest.TestCase):
    def blocked_runner(self):
        """
        Make a `RunnerThread` whose `f` blocks until `self.release` is set, or
        for 10 s, so that waiting for it fails the test instead of hanging.
        """
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        started = threading.Event()
        def f():
            started.set()
            self.release.wait(10)
        rt = RunnerThread(f)
        return rt, started

    def test_interrupt_does_not_wait_for_f(self):
        for exc_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(exc_type = exc_type):
                rt, started = self.blocked_runner()
                t0 = time.monotonic()
                with self.assertRaises(exc_type):
                    with rt:
                        rt.start()
                        started.wait()
                        raise exc_type
                self.assertLess(time.monotonic() - t0, 5)
                # `f` is still running, in a thread that doesn't block exit.
                self.assertTrue(rt._thread.is_alive())
                self.assertTrue(rt._thread.daemon)

    def test_wait_on_interrupt_waits_for_f(self):
        # For sandboxes whose cleanup doesn't end `f`.
        started = threading.Event()
        done = threading.Event()
        def f():
            started.set()
            time.sleep(0.2)
            done.set()
        with self.assertRaises(KeyboardInterrupt):
            with RunnerThread(f, wait_on_interrupt = True) as rt:
                rt.start()
                started.wait()
                raise KeyboardInterrupt
        self.assertTrue(done.is_set())

    def test_other_errors_wait_for_f(self):
        started = threading.Event()
        done = threading.Event()
        def f():
            started.set()
            time.sleep(0.2)
            done.set()
        with self.assertRaises(ValueError):
            with RunnerThread(f) as rt:
                rt.start()
                # Before `f` starts, an error skips it instead.
                started.wait()
                raise ValueError
        self.assertTrue(done.is_set())


# Like an agent safety step: the main thread serves the HTTP API while codex
# (here a long sleep) runs in the runner thread, inside the `finally` that
# stops the sandbox container.
FAKE_SAFETY_STEP = '''
import time
from crisp import http_server
from crisp.__main__ import install_exit_handlers
install_exit_handlers()
def codex(api_port, api_key):
    print('codex running', flush=True)
    time.sleep(60)
    print('codex finished', flush=True)
try:
    http_server.run_with_callbacks(http_server.example_build_app, codex)
finally:
    print('container stopped', flush=True)
'''


class SignalDuringAgentRunTest(unittest.TestCase):
    def test_signal_stops_the_container_without_waiting_for_codex(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # SIGTERM exits through `sys.exit`; on an uncaught `KeyboardInterrupt`,
        # Python exits by killing itself with SIGINT.
        for sig, returncode in ((signal.SIGTERM, 128 + signal.SIGTERM),
                (signal.SIGINT, -signal.SIGINT)):
            with self.subTest(sig = sig):
                p = subprocess.Popen([sys.executable, '-c', FAKE_SAFETY_STEP],
                    cwd = repo, stdout = subprocess.PIPE,
                    stderr = subprocess.DEVNULL, text = True)
                self.addCleanup(p.kill)
                self.assertEqual(p.stdout.readline(), 'codex running\n')
                p.send_signal(sig)
                out, _ = p.communicate(timeout = 10)
                self.assertEqual(out, 'container stopped\n')
                self.assertEqual(p.returncode, returncode)


if __name__ == '__main__':
    unittest.main()
