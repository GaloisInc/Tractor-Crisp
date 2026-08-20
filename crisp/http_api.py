import asyncio
from contextlib import asynccontextmanager
import socket
import threading

from fastapi import FastAPI
import hypercorn.asyncio
from hypercorn.config import Config as HypercornConfig

from .util import ScopedThread


class RunnerThread:
    """
    Lifecycle management for the runner thread.  Expected usage:

        with RunnerThread(f) as rt:
            # Main thread setup...
            rt.start()
            # ...
            x = rt.result()

    The background thread is started immediately, but `f` won't be run until
    `start` is called, so the main thread can do any necessary setup (namely,
    starting up the HTTP server).  If an exception occurs prior to `start()`,
    `f` will not be called at all.  Otherwise, if `f` is started but throws an
    exception, the exception will be propagated when `result()` is called or on
    exit from the `with` block, whichever comes first.
    """

    def __init__(self, f):
        self._result = None
        self._exc = None
        self._start_event = threading.Event()
        self._exit_early = False
        self._on_finish = None

        def run():
            print('runner prestart')
            self._start_event.wait()
            if self._exit_early:
                print('runner early exit')
                return
            print('runner start')
            try:
                self._result = f()
            except BaseException as e:
                self._exc = e
            if self._on_finish is not None:
                self._on_finish()
            print('runner finish')

        self._thread = threading.Thread(target=run)
        self._thread.start()

    def _take_exc(self):
        exc = self._exc
        self._exc = None
        return exc

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # Have the background thread exit early if possible.  This will usually
        # have no effect if `start()` was already called, though it may in the
        # rare case where `start` was called but the background thread hasn't
        # woken up and checked `_exit_early` yet.  But this is only a fast path
        # in case of an exception during main-thread setup; if `f` has already
        # started running, the `join()` below will wait for it to finish.
        self._exit_early = True
        # If `start` was not called yet, we trigger the start event here so
        # that the background thread can make progress and `join()` won't block
        # forever.
        self._start_event.set()

        self._thread.join()

        exc = self._take_exc()
        if exc is not None:
            if exc_type is None:
                raise exc
            else:
                print(f'warning: discarding runner thread exception {exc!r}')
        return False

    def start(self, on_finish=None):
        self._on_finish = on_finish
        self._start_event.set()

    def result(self):
        self._thread.join()
        exc = self._take_exc()
        if exc is not None:
            raise exc
        return self._result


def example_build_app(app: FastAPI):
    @app.get('/time')
    async def get_time():
        from datetime import datetime
        return {'time': datetime.now().astimezone().isoformat()}


def run_with_callbacks(build_app, f, *args, **kwargs):
    """
    Run `f(api_port, *args, **kwargs)` on a background thread, where `api_port`
    is a port number on `localhost` where the client can access `fa`.  Returns
    the result of the call to `f`, or propagates any exception `f` raises.
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 0))
        api_port = listener.getsockname()[1]
        #listener.listen(socket.SOMAXCONN)

        with RunnerThread(lambda: f(api_port, *args, **kwargs)) as rt:
            async def async_run():
                loop = asyncio.get_running_loop()
                finish_event = asyncio.Event()

                @asynccontextmanager
                async def lifespan(_app: FastAPI):
                    def on_finish():
                        loop.call_soon_threadsafe(finish_event.set)
                    rt.start(on_finish = on_finish)
                    yield

                app = FastAPI(lifespan=lifespan)
                build_app(app)

                config = HypercornConfig()
                # Hypercorn takes ownership of the file descriptor, so use
                # `release()` to avoid a double close.
                config.bind = [f'fd://{listener.detach()}']
                await hypercorn.asyncio.serve(
                        app, config, shutdown_trigger=finish_event.wait)

            asyncio.run(async_run())

            return rt.result()
