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
        self.node_id = node_id

    def __str__(self):
        msg = self.message
        if self.node_id is not None:
            msg += f'\nnode_id: {self.node_id}'
        return msg
