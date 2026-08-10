from collections.abc import Iterator
from . import mvir as mvir_module
from .mvir import MVIR, Node, TreeNode


# Node kinds and keys that refer to the output of an operation.  This is used
# to find the operation that produced a given `TreeNode`.  These are compared
# against index entries, so entries here must cover the unmigrated forms of
# nodes.
OUTPUT_KEYS = {
    mvir_module.LlmOpNode.KIND: 'new_code',
    mvir_module.CodexAgentOpNode.KIND: 'new_code',
    # Backward compatibility with unmigrated `CodexAgentOp`s
    'codex_agent_op': 'new_code',
}

OUTPUT_KEYS_SET = set((k, v) for k, v in OUTPUT_KEYS.items())

# Node kinds and keys that refer to the input of an operation.  These are used
# after the node in question has been loaded and migrated, so there's no need
# to cover the unmigrated forms here.
INPUT_KEYS = {
    mvir_module.LlmOpNode.KIND: 'old_code',
    mvir_module.CodexAgentOpNode.KIND: 'old_code',
}


def predecessors(mvir: MVIR, target: TreeNode) -> Iterator[tuple[TreeNode, Node]]:
    """
    Iterate over predecessors of `target`.  Yields pairs `(pred, op)`, where
    `op` is the operation that transformed `pred` into `target`.
    """
    for ie in mvir.index(target.node_id()):
        if (ie.kind, ie.key) not in OUTPUT_KEYS_SET:
            continue
        op = mvir.node(ie.node_id)
        input_key = INPUT_KEYS.get(op.kind)
        if input_key is None:
            continue
        pred = mvir.node(getattr(op, input_key))
        yield (pred, op)

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
    seen = set()
    while True:
        seen.add(tree.node_id())

        found = False
        for pred, op in predecessors(mvir, tree):
            # If this would create a cycle, try a different branch.  In
            # particular, given ops `A->B`, `B->B`, and `B->C`, this will
            # result in a history containing only `A->B` and `B->C`, omitting
            # the self-loop `B->B`.
            if pred.node_id() in seen:
                continue

            entries.append((tree, op))
            tree = pred
            found = True
            break

        if not found:
            break

    entries.append((tree, None))
    return entries
