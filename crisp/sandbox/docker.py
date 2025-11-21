from contextlib import contextmanager
import docker
import io
import os
import sys
import tarfile

from ..mvir import FileNode, TreeNode
from ..util import ChunkPrinter


class WorkContainer:
    """
    Helper for managing a Docker/Podman container for building and running user
    code.

    The usual workflow with this type is to populate the directory with one or
    more inputs from MVIR using `checkout` methods, run some command on the
    inputs by calling `run`, and store the outputs back into MVIR using the
    `commit` methods.
    """
    def __init__(self, mvir):
        self.mvir = mvir
        self.client = docker.from_env()
        try:
            self.image = self.client.images.get('tractor-crisp-user')
        except:
            print('error getting image `tractor-crisp-user` - you may need to build it first')
            raise
        self.container = None

    def start(self):
        self.container = self.client.containers.run(
                self.image, ('sleep', '1000'), detach=True, remove=True)

    def stop(self):
        if self.container is not None:
            self.container.stop(timeout=1)
            #self.container.remove(v=True)

    def _checkout_tar_file(self, tar_bytes):
        self.container.put_archive('/root/work/', tar_bytes)

    def checkout(self, n_tree):
        assert isinstance(n_tree, TreeNode)
        tar_io = io.BytesIO()
        with tarfile.open(fileobj=tar_io, mode='w') as t:
            for rel_path, n_file_id in n_tree.files.items():
                n_file = self.mvir.node(n_file_id)
                info = tarfile.TarInfo(rel_path)
                info.size = len(n_file.body())
                t.addfile(info, io.BytesIO(n_file.body()))
        self._checkout_tar_file(tar_io.getvalue())

    def checkout_file(self, rel_path, n_file):
        assert not os.path.isabs(rel_path)
        assert isinstance(n_file, FileNode)
        tar_io = io.BytesIO()
        with tarfile.open(fileobj=tar_io, mode='w') as t:
            info = tarfile.TarInfo(rel_path)
            info.size = len(n_file.body())
            t.addfile(info, io.BytesIO(n_file.body()))
        self._checkout_tar_file(tar_io.getvalue())

    def commit_dir(self, rel_path):
        assert not os.path.isabs(rel_path)
        tar_bytes_iter, st = self.container.get_archive(self.join(rel_path))
        tar_bytes = b''.join(tar_bytes_iter)
        tar_io = io.BytesIO(tar_bytes)
        files = {}
        # If the user calls `commit_dir('foo/bar'), we want to produce a
        # `TreeNode` with file names like `foo/bar/README.txt`.  However, the
        # paths returned by `get_archive` are prefixed with just the basename
        # of the requested path, like `bar/README.txt`.  We add this prefix to
        # get the desired path.
        dest_prefix = os.path.dirname(rel_path)
        with tarfile.open(fileobj=tar_io, mode='r') as t:
            while (info := t.next()) is not None:
                match info.type:
                    case tarfile.REGTYPE:
                        pass
                    case tarfile.DIRTYPE:
                        continue
                    case t:
                        raise ValueError('expected REGTYPE or DIRTYPE, but got %r' % (t,))
                f = t.extractfile(info)
                dest_path = os.path.normpath(os.path.join(dest_prefix, info.name))
                assert dest_path not in files, 'duplicate entry for %s' % dest_path
                files[dest_path] = FileNode.new(self.mvir, f.read()).node_id()
        return TreeNode.new(self.mvir, files=files)

    def commit_file(self, rel_path):
        assert not os.path.isabs(rel_path)
        tar_bytes_iter, st = self.container.get_archive(self.join(rel_path))
        tar_bytes = b''.join(tar_bytes_iter)
        tar_io = io.BytesIO(tar_bytes)
        with tarfile.open(fileobj=tar_io, mode='r') as t:
            info = t.next()
            expect_name = os.path.basename(rel_path)
            assert info.name == expect_name, \
                    'expected tar file to contain %r, but got %r' % (expect_name, info.name)
            f = t.extractfile(info)
            n = FileNode.new(self.mvir, f.read())
            assert t.next() is None, 'expected only one file in commit_file output'
            return n

    def join(self, *args, **kwargs):
        return os.path.join('/root/work', *args, **kwargs)

    def run(self, cmd, shell=False, stream=False):
        if shell:
            assert isinstance(cmd, str)
            cmd = ['sh', '-c', cmd]
        if isinstance(cmd, tuple):
            # `exec_run` requires either a list or str, not a tuple.
            cmd = list(cmd)

        if not stream:
            exit_code, logs = self.container.exec_run(
                    cmd, workdir='/root/work', stream=stream)
            sys.stdout.flush()
            sys.stdout.buffer.write(logs)
            sys.stdout.flush()
            return exit_code, logs

        # High-level `exec_run` API doesn't return the exit code when streaming
        # is enabled, so use the low-level API instead.
        exec_info = self.client.api.exec_create(
                self.container.id, cmd, workdir='/root/work')
        exec_id = exec_info['Id']
        stream = self.client.api.exec_start(exec_id, stream=True)

        p = ChunkPrinter()
        acc = bytearray()
        for data in stream:
            p.write_bytes(data)
            p.flush()
            p.increment()
            acc.extend(data)
        p.finish()

        logs = bytes(acc)
        exit_code = self.client.api.exec_inspect(exec_id).get('ExitCode')

        return exit_code, logs


KEEP_WORK_CONTAINER = False

@contextmanager
def run_work_container(cfg, mvir):
    wc = WorkContainer(mvir)
    wc.start()
    yield wc
    if not KEEP_WORK_CONTAINER:
        wc.stop()
    else:
        print('keeping work container %r' % (wc.container.name,))

def set_keep_work_container(keep):
    global KEEP_WORK_CONTAINER
    KEEP_WORK_CONTAINER = keep
