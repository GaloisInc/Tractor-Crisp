from contextlib import contextmanager
import io
import os
import tarfile

from .mvir import FileNode, TreeNode


CONTAINER_RUNTIME = os.environ.get('CRISP_CONTAINER_RUNTIME', 'podman')

def _mk_client():
    match CONTAINER_RUNTIME:
        case 'docker':
            import docker
            return docker.from_env()
        case 'podman':
            import podman
            return podman.client.from_env()
        case x:
            raise ValueError('unknown CRISP_CONTAINER_RUNTIME: %r' % (x,))

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
        self.client = _mk_client()
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
            self.container.stop(timeout=5)
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
                files[info.name] = FileNode.new(self.mvir, f.read()).node_id()
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

    def run(self, cmd, shell=False):
        if shell:
            assert isinstance(cmd, str)
            cmd = ['sh', '-c', cmd]
        if isinstance(cmd, tuple):
            # `exec_run` requires either a list or str, not a tuple.
            cmd = list(cmd)

        exit_code, logs = self.container.exec_run(cmd, workdir='/root/work')
        parsed_logs = b''.join(data for fd, data in _log_entries(logs))
        return exit_code, parsed_logs


def _log_entries(b):
    i = 0

    while i < len(b):
        fd = b[i]
        data_size = int.from_bytes(b[i + 1 : i + 8], byteorder='big', signed=False)
        i += 8
        start = i
        i += data_size
        end = i
        data = b[start : end]
        yield fd, data


KEEP_WORK_CONTAINER = False

@contextmanager
def run_work_container(cfg, mvir):
    wc = WorkContainer(mvir)
    wc.start()
    print('status', wc.container.status)
    yield wc
    print('status', wc.container.status)
    if not KEEP_WORK_CONTAINER:
        wc.stop()
    else:
        print('keeping work container %r' % (wc.container.name,))

def set_keep_work_container(keep):
    global KEEP_WORK_CONTAINER
    KEEP_WORK_CONTAINER = keep
