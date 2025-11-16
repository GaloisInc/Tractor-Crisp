import os
import pygit2
from typing import Optional

from . import mvir as mvir_module
from .mvir import MVIR, TreeNode, FileNode


def repo_path(mvir: MVIR) -> str:
    return os.path.join(mvir._path, "git")


def get_repo(mvir: MVIR) -> pygit2.Repository:
    path = repo_path(mvir)

    try:
        return pygit2.Repository(
            path,
            pygit2.GIT_REPOSITORY_OPEN_NO_SEARCH
            | pygit2.GIT_REPOSITORY_OPEN_BARE
            | pygit2.GIT_REPOSITORY_OPEN_NO_DOTGIT,
        )
    except pygit2.GitError:
        return pygit2.init_repository(
            path,
            pygit2.GIT_REPOSITORY_INIT_BARE
            | pygit2.GIT_REPOSITORY_INIT_NO_REINIT
            | pygit2.GIT_REPOSITORY_INIT_NO_DOTGIT_DIR,
        )


# For each MVIR node kind that represents a code transformation, this gives the
# names of the fields containing the old and new `TreeNode`s.
OP_NODE_KINDS = {
    mvir_module.LlmOpNode.KIND: ("old_code", "new_code"),
}

HISTORY_INDEX_KEYS = set((kind, new) for (kind, (old, new)) in OP_NODE_KINDS.items())


def render(mvir: MVIR, target: TreeNode) -> pygit2.Oid:
    """
    Generate git history representing the steps that produced `target`, and
    return the git object ID of the final commit.
    """
    trees = [target]
    ops = []
    while True:
        # Try to find an op that produced the last tree.
        tree = trees[-1]
        op_node_id = None
        for ie in mvir.index(tree.node_id()):
            if (ie.kind, ie.key) in HISTORY_INDEX_KEYS:
                op_node_id = ie.node_id
                break
        if op_node_id is None:
            # `tree` is the oldest tree we could find in the history.
            break

        op = mvir.node(op_node_id)
        old_key, _ = OP_NODE_KINDS[op.kind]
        old_tree_node_id = getattr(op, old_key)
        old_tree = mvir.node(old_tree_node_id)
        ops.append(op)
        trees.append(old_tree)

    assert len(ops) == len(trees) - 1

    repo = get_repo(mvir)
    commit = commit_tree(mvir, repo, trees[-1], "initial commit")

    for tree, op in zip(reversed(trees[:-1]), reversed(ops)):
        msg = op.kind  # TODO
        commit = commit_tree(mvir, repo, tree, msg, parent=commit)

    return commit


def commit_tree(
    mvir: MVIR,
    repo: pygit2.Repository,
    tree: TreeNode,
    msg: str,
    parent: Optional[pygit2.Oid] = None,
) -> pygit2.Oid:
    # Convert the `TreeNode`'s flat path->ID mapping to a nested structure like
    # the one used by git tree objects.  At each level, each entry is either a
    # `FileNode` or a dict representing a directory.
    tree_files = {}

    def get_parent_and_name(path):
        head, tail = os.path.split(path)
        if head == "":
            return tree_files, path
        else:
            grandparent, parent_name = get_parent_and_name(head)
            if parent_name not in grandparent:
                grandparent[parent_name] = {}
            return grandparent[parent_name], tail

    for path, node_id in tree.files.items():
        dct, name = get_parent_and_name(path)
        dct[name] = mvir.node(node_id)

    def build_tree(dct):
        tb = repo.TreeBuilder()
        for name, x in dct.items():
            if isinstance(x, dict):
                tb.insert(name, build_tree(x), pygit2.GIT_FILEMODE_TREE)
            else:
                assert isinstance(x, FileNode)
                blob = repo.create_blob(x.body())
                tb.insert(name, blob, pygit2.GIT_FILEMODE_BLOB)
        return tb.write()

    git_tree = build_tree(tree_files)

    meta = []
    for ie in mvir.index(tree.node_id()):
        n = mvir.node(ie.node_id)
        if isinstance(n, mvir_module.TestResultNode):
            meta.append("test exit code = %d" % n.exit_code)
        elif isinstance(n, mvir_module.FindUnsafeAnalysisNode):
            j_unsafe = n.body_json()
            unsafe_count = sum(
                len(file_info["internal_unsafe_fns"])
                + len(file_info["fns_containing_unsafe"])
                for file_info in j_unsafe.values()
            )
            meta.append("unsafe count = %d" % unsafe_count)

    if len(meta) > 0:
        msg = msg + "\n\n" + "\n".join(meta)

    sig = pygit2.Signature("CRISP", "crisp@example.com", 0, 0, "utf-8")
    commit = repo.create_commit(
        None, sig, sig, msg, git_tree, [parent] if parent is not None else []
    )
    return commit
