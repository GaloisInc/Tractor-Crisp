import contextlib
from typing import Callable


class CrispError(Exception):
    """
    Custom error type for CRISP operations.  Operations in `analysis`, `llm`,
    `workflow`, etc, should throw `CrispError` when something goes wrong in a
    way that can't reasonably be recovered from locally, such as an LLM query
    producing malformed output.  The error may be caught at a higher level to
    attempt recovery there if possible.

    If CRISP operations throw any kind of exception other than `CrispError`,
    that indicates a bug in CRISP.
    """

    def __init__(self, message, node_id = None):
        super().__init__(message)
        self.message = message

        from .mvir import Node
        if isinstance(node_id, Node):
            node_id = node_id.node_id()
        self.node_id = node_id

    def __str__(self):
        msg = self.message
        if self.node_id is not None:
            msg += f'\nnode_id: {self.node_id}'
        return msg

class ValidateError(Exception):
    """
    Special exception type used in validation functions to provide extra
    context regarding an error.  This is meant to be used with `raise from`:

        try:
            process(foo)
        except ValueError as e:
            raise ValidateError('while processing foo') as e

    The final `ValidateError` will have a message like "while processing foo:
    while parsing bar: ValueError: baz".
    """
    def __str__(self):
        msg = super().__str__()
        inner = self.__cause__
        if inner is None:
            return msg
        elif isinstance(inner, ValidateError):
            return f'{msg}: {inner}'
        else:
            return f'{msg}: {type(inner).__name__}: {inner}'

class ValidateErrorCollector:
    """
    Helper for collecting multiple validation errors.  We want to report as
    many errors as possible so the agent can correct them all at once.  Common
    recoverable errors should be reported with `err()` or `err(msg)` so
    validation can continue and potentially find more errors later in the
    process.  Uncommon errors can still raise exceptions as normal; common
    exceptions will be converted to `ValidateError` to attach the current
    validation context to the message.
    """

    def __init__(self):
        self.ctx = []
        self.errors = []

    @contextlib.contextmanager
    def context(self, f: Callable[[], str]):
        """
        Push a context function.  If an error occurs inside this context, the
        function will be called to get a string description of the context.
        """
        self.ctx.append(f)
        try:
            yield
        except (KeyError, ValueError, TypeError, AttributeError) as e:
            # Convert common errors to `ValidateError`, which attaches the
            # current context description.
            raise ValidateError(f()) from e
        finally:
            self.ctx.pop()

    def err(self, e):
        """
        Record an error whose message is derived from the exception `e`.
        """
        self.err_msg(f'{type(e).__name__}: {e}')

    def err_msg(self, s):
        """
        Record an error with message `s`.
        """
        if len(self.ctx) > 0:
            ctx_str = ': '.join(f() for f in self.ctx) + ': '
        else:
            ctx_str = ''
        self.errors.append(ctx_str + s)
