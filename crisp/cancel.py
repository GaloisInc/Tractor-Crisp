from dataclasses import dataclass
import itertools
from typing import Callable
import threading


@dataclass
class CancelSlot:
    # Callback to run to cancel the thread that owns this `CancelSlot`.  Note
    # this function will be called from a different thread.
    cancel_action: Callable[[]] | None = None
    canceled: bool = False

    def poll(self):
        if self.canceled:
            raise Exception('thread was already canceled')

    def set_cancel_action(self, f):
        self.poll()
        self.cancel_action = f

    def cancel(self):
        if not self.canceled:
            self.canceled = True
            if self.cancel_action is not None:
                self.cancel_action()


_TLS = threading.local()

def poll():
    slot = getattr(_TLS, 'cancel_slot', None)
    if slot is not None:
        slot.poll()

def set_cancel_action(f):
    """
    Set the cancel action for the current thread to `f`.

    If the current thread is not cancelable, this has no effect.
    """
    slot = getattr(_TLS, 'cancel_slot', None)
    if slot is not None:
        slot.set_cancel_action(f)

def set_cancel_slot(slot):
    _TLS.cancel_slot = slot


class CancelableThread(threading.Thread):
    """
    A thread that can be canceled.  The thread itself must register a
    cancellation action by calling `cancel.set_cancel_action(f)`; afterward,
    calls to `thread.cancel()` will run the cancellation action.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancel_slot = CancelSlot()

    def cancel(self):
        self.cancel_slot.cancel()

    def run(self):
        _TLS.cancel_slot = self.cancel_slot
        try:
            super().run()
        finally:
            # Make sure canceling a thread that already exited is a no-op.
            self.cancel_slot.cancel_action = None


class CancelableScopedThread(CancelableThread):
    def __init__(self, *args, thread_scope, **kwargs):
        super().__init__(*args, **kwargs)
        self.scope = thread_scope

    def run(self):
        try:
            super().run()
        except BaseException as e:
            self.scope._thread_exc(e)

class CancelableThreadScope:
    def __init__(self):
        # This lock protects `threads` and `first_exc`.
        self.lock = threading.Condition()
        # Append-only list of all threads we've created.
        self.threads = []
        # First exception reported by any child thread.  If multiple threads
        # exit with exceptions, we re-raise only the first.
        self.first_exc = None
        self.closed = False

    def spawn(self, *args, **kwargs) -> CancelableScopedThread:
        with self.lock:
            if self.closed:
                raise RuntimeError('thread scope is closed')
            th = CancelableScopedThread(*args, thread_scope = self, **kwargs)
            self.threads.append(th)
            try:
                th.start()
            except BaseException:
                self.threads.remove(th)
                raise
            return th

    def _join_inner(self):
        """
        Block until there are no more threads in this scope.  This returns the
        first exception that was produced by any child thread, or `None` if
        none of them exited with an exception.
        """
        for i in itertools.count():
            with self.lock:
                if i >= len(self.threads):
                    self.closed = True
                    return self.first_exc
                th = self.threads[i]
            th.join()

    def join(self):
        """
        Block until there are no more threads in this scope.  If any thread
        exited with an exception, this re-raises the exception.
        """
        exc = self._join_inner()
        if exc is not None:
            raise exc

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        with self.lock:
            # Prevent creating more threads in this scope.
            self.closed = True
            # Cancel all remaining threads.
            for th in self.threads:
                try:
                    th.cancel()
                except BaseException as e:
                    print(f'warning: thread.cancel() failed: {e}')

        # Wait for canceled threads to finish.
        child_exc = self._join_inner()

        if child_exc is not None:
            if exc_value is None:
                raise child_exc
            else:
                print(f'warning: dropping extra exception {child_exc}')

    def _thread_exc(self, e):
        with self.lock:
            if self.first_exc is None:
                self.first_exc = e
            else:
                print(f'warning: dropping extra exception {e}')
