# FIXME: copied from crisp.git - update crisp.git to use this instead

from . import mvir as mvir_module
from .mvir import MVIR, Node, TreeNode

# For each MVIR node kind that represents a code transformation, this gives the
# names of the fields containing the old and new `TreeNode`s.
OP_NODE_KINDS = {
    mvir_module.LlmOpNode.KIND: ('old_code', 'new_code'),
    mvir_module.CodexAgentOpNode.KIND: ('old_code', 'new_code'),

    # Backward compatibility with unmigrated `CodexAgentOp`s
    'codex_agent_op': ('old_code', 'new_code'),
}

HISTORY_INDEX_KEYS = set((kind, new) for (kind, (old, new)) in OP_NODE_KINDS.items())

def get_history(mvir: MVIR, target: TreeNode) -> list[tuple[TreeNode, Node | None]]:
    """
    Return a list of the steps that produced `target`.  Each entry contains the
    `TreeNode` representing the state of the code and the op `Node` that
    produced it (or `None` for the initial state).  Returns the history in
    reverse order, so the first entry of the result is `target` and the last is
    the initial state.
    """
    tree = target
    entries = []
    while True:
        # Find the op that produced `tree`
        op_node_id = None
        for ie in mvir.index(tree.node_id()):
            if (ie.kind, ie.key) in HISTORY_INDEX_KEYS:
                op_node_id = ie.node_id
                break
        if op_node_id is None:
            # `tree` is the oldest tree we could find in the history.
            break

        op = mvir.node(op_node_id)
        entries.append((tree, op))

        old_key, _ = OP_NODE_KINDS[op.kind]
        old_tree_node_id = getattr(op, old_key)
        old_tree = mvir.node(old_tree_node_id)
        tree = old_tree

    entries.append((tree, None))
    return entries
