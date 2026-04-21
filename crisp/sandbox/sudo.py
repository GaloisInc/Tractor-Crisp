from contextlib import contextmanager
import io
import os
from pathspec.pathspec import PathSpec
import pwd
import shlex
import subprocess
import sys
import tarfile
from subprocess import CompletedProcess, Popen

from ..mvir import FileNode, TreeNode
from ..util import ChunkPrinter


class SudoSandbox:
    """
    Helper for managing a `sudo`-based sandbox.  This uses `sudo` to run
    commands as an unprivileged user.
    """
    def __init__(self, mvir, user):
        self.mvir = mvir
        self.user = user

        # Get the numeric ID of the unprivileged user.
        entry = pwd.getpwnam(user)
        dir_name = 'crisp_sandbox_%d' % entry.pw_uid
        self.dir_path = os.path.join(os.environ.get('TMPDIR', '/tmp'), dir_name)

    def _sudo_cmd(self, cmd, env):
        env_args = (f"{k}={v}" for (k, v) in env.items())
        return ('sudo', '-u', self.user, *env_args, *cmd)

    def _run_sudo(self, cmd, check=True, env={}, **kwargs) -> CompletedProcess[str]:
        sudo_cmd = self._sudo_cmd(cmd, env)
        p = subprocess.run(sudo_cmd, check=check, **kwargs)
        return p

    def _popen_sudo(self, cmd, env={}, **kwargs) -> Popen[str]:
        sudo_cmd = self._sudo_cmd(cmd, env)
        p = subprocess.Popen(sudo_cmd, **kwargs)
        return p

    def start(self):
        # This command will error if the directory already exists.
        p = self._run_sudo(('mkdir', self.dir_path))

    def stop(self):
        if not KEEP_TEMP_DIR:
            self._run_sudo(('rm', '-rf', self.dir_path))
        else:
            print('keeping temp dir %r' % self.dir_path)

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
        self._run_sudo(('tar', '-C', self.dir_path, '-x'), input=tar_io.getvalue())

    def checkout_file(self, rel_path, n_file):
        assert not os.path.isabs(rel_path)
        assert isinstance(n_file, FileNode)
        file_path = self.join(rel_path)
        cmd = 'mkdir -p {parent_path} && exec cat >{file_path}'.format(
            parent_path=shlex.quote(os.path.dirname(file_path)),
            file_path=shlex.quote(file_path),
        )
        self._run_sudo(('sh', '-c', cmd), input=n_file.body())

    def commit_dir(self, rel_path, ignore_spec: PathSpec | None = None):
        assert not os.path.isabs(rel_path)
        p = self._run_sudo(('tar', '-C', self.join(rel_path), '-c', '.'), stdout=subprocess.PIPE)
        tar_bytes = p.stdout
        tar_io = io.BytesIO(tar_bytes)
        files = {}
        with tarfile.open(fileobj=tar_io, mode='r') as t:
            while (info := t.next()) is not None:
                match info.type:
                    case tarfile.REGTYPE:
                        pass
                    case tarfile.LNKTYPE:
                        # Extract hard links for now, cargo creates some
                        pass
                    case tarfile.DIRTYPE:
                        continue
                    case t:
                        raise ValueError(f"expected REGTYPE, LNKTYPE or DIRTYPE, but got {t} for file {info.name}")
                if ignore_spec is not None and ignore_spec.match_file(info.name):
                    continue
                f = t.extractfile(info)
                # Prefix output paths with the requested `rel_path`.
                dest_path = os.path.normpath(os.path.join(rel_path, info.name))
                files[dest_path] = FileNode.new(self.mvir, f.read()).node_id()
        return TreeNode.new(self.mvir, files=files)

    def commit_file(self, rel_path):
        assert not os.path.isabs(rel_path)
        file_path = self.join(rel_path)
        p = self._run_sudo(('cat', file_path), stdout=subprocess.PIPE)
        n = FileNode.new(self.mvir, p.stdout)
        return n

    def join(self, *args, **kwargs):
        return os.path.join(self.dir_path, *args, **kwargs)

    def run(self, cmd, shell=False, stream=False, cwd: str = ".", env={}) -> tuple[int, str | bytes]:
        if shell:
            assert isinstance(cmd, str)
            cmd = ['sh', '-c', cmd]

        print(f"cd {shlex.quote(self.join(cwd))} && {shlex.join(cmd)}")

        cmd = 'cd {dir_path} && {cmd}'.format(
            dir_path=shlex.quote(self.join(cwd)),
            cmd=shlex.join(cmd),
        )
        cmd = ('sh', '-c', cmd)

        # Copy the API key into the sandbox only if already set outside,
        # but do not override the value from env.
        if 'CRISP_API_KEY' not in env:
            api_key = os.environ.get('CRISP_API_KEY')
            if api_key is not None:
                env['CRISP_API_KEY'] = api_key

        if not stream:
            p = self._run_sudo(cmd, check=False, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            return p.returncode, p.stdout

        p = self._popen_sudo(cmd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        printer = ChunkPrinter()
        acc = bytearray()
        while True:
            data = p.stdout.read(4096)
            if len(data) == 0:
                break
            printer.write_bytes(data)
            printer.flush()
            printer.increment()
            acc.extend(data)
        printer.finish()

        p.wait()

        logs = bytes(acc)

        return p.returncode, logs


KEEP_TEMP_DIR = False

@contextmanager
def run_sandbox(cfg, mvir):
    user = os.environ['CRISP_SANDBOX_SUDO_USER']
    sb = SudoSandbox(mvir, user)
    sb.start()
    try:
        yield sb
    finally:
        sb.stop()

def set_keep_temp_dir(keep):
    global KEEP_TEMP_DIR
    KEEP_TEMP_DIR = keep
