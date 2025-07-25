from contextlib import contextmanager
import glob
import os
import shutil
from typing import Union, Sequence

from .mvir import FileNode, TreeNode

class WorkDir:
    """
    Helper for manipulating the contents of a work directory.

    For operations that take an MVIR `TreeNode` as input and produce a new
    `TreeNode` as output by running a shell command, we copy the input files
    into a temporary work directory to reduce the chances that the command will
    be influenced by other untracked files in the main project directory.  This
    helps with reproducibility and also lets us create or modify files as
    needed without worrying about overwriting the user's data.

    The usual workflow with this type is to populate the directory with one or
    more inputs from MVIR using `checkout` methods, run some command on the
    inputs, and store the outputs back into MVIR using the `commit` methods.
    """
    def __init__(self, mvir, path):
        self.mvir = mvir
        self.path = path

    def checkout(self, n_tree):
        assert isinstance(n_tree, TreeNode)
        for rel_path, n_file_id in n_tree.files.items():
            n_file = self.mvir.node(n_file_id)
            self.checkout_file(rel_path, n_file)

    def checkout_file(self, rel_path, n_file):
        assert not os.path.isabs(rel_path)
        assert isinstance(n_file, FileNode)
        path = os.path.join(self.path, rel_path)
        assert not os.path.exists(path), \
            'path %r already exists in work dir %r' % (rel_path, self.path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(n_file.body())

    def commit(self, globs: Union[str, Sequence[str]]):
        if isinstance(globs, str):
            globs = (globs,)
        all_rel_paths = set(os.path.normpath(rel_path)
            for g in globs
            for rel_path in glob.glob(g, root_dir=self.path, recursive=True))
        dct = {}
        for rel_path in all_rel_paths:
            assert rel_path not in dct
            dct[rel_path] = self.commit_file(rel_path).node_id()
        return TreeNode.new(self.mvir, files=dct)

    def commit_file(self, rel_path):
        assert not os.path.isabs(rel_path)
        path = os.path.join(self.path, rel_path)
        assert os.path.exists(path)
        with open(path, 'rb') as f:
            return FileNode.new(self.mvir, f.read())

    def join(self, *args, **kwargs):
        return os.path.join(self.path, *args, **kwargs)

KEEP_WORK_DIR = False

@contextmanager
def lock_work_dir(cfg, mvir):
    """
    Create a work directory based on `cfg`, and delete it on exit from the
    context manager.  This function raises an exception if the directory
    already exists.  As long as all processes follow this protocol, only one
    process can be inside the context manager at a time, so there's no risk of
    one process overwriting another process's files.
    """
    work_dir = os.path.join(cfg.mvir_storage_dir, 'work')
    # If the directory already exists, some other process holds the lock.
    os.makedirs(work_dir, exist_ok=False)
    try:
        yield WorkDir(mvir, work_dir)
    finally:
        if not KEEP_WORK_DIR:
            shutil.rmtree(work_dir)

def set_keep_work_dir(keep):
    global KEEP_WORK_DIR
    KEEP_WORK_DIR = keep
