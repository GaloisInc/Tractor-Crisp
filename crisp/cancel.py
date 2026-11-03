from dataclasses import dataclass
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
        super().run()


class CancelableScopedThread(CancelableThread):
    def __init__(self, *args, *, scope, **kwargs):
        super().__init__(*args, **kwargs)
        self.scope = scope

    def run(self):
        try:
            super().run()
            self.scope._thread_done(self)
        except BaseException as e:
            self.scope._thread_exc(self, e)

class CancelableThreadScope:
    def __init__(self):
        # This lock protects `threads` and `first_exc`.
        self.lock = threading.Lock()
        self.threads = []
        self.first_exc = None

    def spawn(self, *args, **kwargs) -> CancelableScopedThread:
        with self.lock:
            th = CancelableScopedThread(*args, scope = self, **kwargs)
            self.threads.append(th)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        with self.lock:
            if exc_value is not None:
                if self.first_exc is None:
                    self.first_exc = exc_value
                else:
                    print(f'warning: dropping extra exception {exc_value}')

            for th in self.threads:
                th.cancel()

        while True:
            # Pick a thread and wait for it to exit.  The thread will either
            # call `_thread_done` or `_thread_exc` before it exits, so it
            # should have removed itself from `self.threads` before the next
            # iteration.
            with self.lock:
                if len(self.threads) == 0:
                    break
                th = self.threads[0]
            th.join()

        with self.lock:
            if self.first_exc is not None:
                raise self.first_exc
            else:
                return False

    def _thread_done(self, th):
        with self.lock:
            idx = self.threads.index(th)
            del self.threads[idx]

    def _thread_exc(self, th, e):
        with self.lock:
            idx = self.threads.index(th)
            del self.threads[idx]

            if self.first_exc is None:
                self.first_exc = e
            else:
                print(f'warning: dropping extra exception {e}')

            for th in self.threads:
                th.cancel()
