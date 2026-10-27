from contextlib import contextmanager
import io
import os
from pathspec.pathspec import PathSpec
import pwd
import secrets
import shlex
import subprocess
import sys
import tarfile
import threading
import time
from typing import Callable
from subprocess import CompletedProcess, Popen

from ..mvir import FileNode, TreeNode
from ..util import ChunkPrinter


class SudoSandbox:
    """
    Helper for managing a `sudo`-based sandbox.  This uses `sudo` to run
    commands as an unprivileged user.
    """

    # Address used to access services on the host machine
    HOST_ADDR = '127.0.0.1'

    def __init__(self, mvir, user, dir_suffix = None):
        self.mvir = mvir
        self.user = user

        # Get the numeric ID of the unprivileged user.
        entry = pwd.getpwnam(user)
        dir_name = 'crisp_sandbox_%d' % entry.pw_uid
        if dir_suffix is not None:
            dir_name = f'{dir_name}.sb.{dir_suffix}'
        self.dir_path = os.path.join(os.environ.get('TMPDIR', '/tmp'), dir_name)

        # `sudo` processes that are still running, so `stop` can end them.
        # Commands run on the agent's thread while `stop` runs on the main one.
        self._procs = set()
        self._lock = threading.Lock()
        self._stopped = False

    def _sudo_cmd(self, cmd, env):
        env_args = (f"{k}={v}" for (k, v) in env.items())
        return ('sudo', '-u', self.user, *env_args, *cmd)

    def _run_sudo(self, cmd, check=True, env={}, input=None, **kwargs) -> CompletedProcess[str]:
        if input is not None:
            kwargs['stdin'] = subprocess.PIPE
        p = self._popen_sudo(cmd, env=env, **kwargs)
        # On an exception (e.g. Ctrl-C), `p` stays in `_procs` for `stop`.
        stdout, stderr = p.communicate(input)
        self._forget(p)
        if check and p.returncode != 0:
            raise subprocess.CalledProcessError(p.returncode, p.args, stdout, stderr)
        return CompletedProcess(p.args, p.returncode, stdout, stderr)

    def _popen_sudo(self, cmd, env={}, **kwargs) -> Popen[str]:
        sudo_cmd = self._sudo_cmd(cmd, env)
        with self._lock:
            if self._stopped:
                raise RuntimeError(f'sandbox {self.dir_path!r} is stopped')
            p = subprocess.Popen(sudo_cmd, **kwargs)
            self._procs.add(p)
        return p

    def _forget(self, p):
        with self._lock:
            self._procs.discard(p)

    def start(self):
        # This command will error if the directory already exists.
        p = self._run_sudo(('mkdir', self.dir_path))

    def stop(self):
        with self._lock:
            self._stopped = True
            procs = list(self._procs)
        try:
            if procs and not self._end_commands(procs):
                print(f'warning: commands are still running in {self.dir_path!r}; '
                    'not removing it')
                return
            if not KEEP_TEMP_DIR:
                # Only warn on failure (e.g. something the agent started still
                # writing there), so it doesn't hide the error being handled.
                p = subprocess.run(self._sudo_cmd(('rm', '-rf', self.dir_path), {}))
                if p.returncode != 0:
                    print(f'warning: could not remove {self.dir_path!r}')
            else:
                print('keeping temp dir %r' % self.dir_path)
        except BaseException:
            # Usually another Ctrl-C or signal while crisp is being stopped.
            # Let it through, but say what is left to clean up by hand.
            pids = [p.pid for p in procs if p.poll() is None]
            if pids:
                print(f'warning: exiting without cleaning up processes {pids}')
            if not KEEP_TEMP_DIR:
                print(f'warning: exiting without removing {self.dir_path!r}')
            raise

    def _end_commands(self, procs) -> bool:
        """
        End the commands started by the `sudo` processes `procs`.  Returns
        whether they all ended.
        """
        # crisp can't signal the sandbox user's processes, but it can signal
        # `sudo` (whose real UID is crisp's), and `sudo` relays SIGTERM to its
        # command.  It can't relay SIGKILL, so a command that ignores SIGTERM
        # keeps running, and so can processes a command started that outlive
        # it.  Killing those would mean finding them in `/proc` and killing
        # them as the sandbox user.  codex exits on SIGTERM, and leftovers
        # don't use the API, so we don't.
        for p in procs:
            p.terminate()
        deadline = time.monotonic() + STOP_TIMEOUT
        ended = True
        for p in procs:
            try:
                p.wait(timeout = max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                ended = False
        return ended

    def _checkout_tar_file(self, tar_bytes):
        self.container.put_archive('/root/work/', tar_bytes)

    def checkout(self, n_tree, rel_path=None):
        assert isinstance(n_tree, TreeNode)
        if rel_path is not None:
            assert not os.path.isabs(rel_path)
        tar_io = io.BytesIO()
        with tarfile.open(fileobj=tar_io, mode='w') as t:
            for file_path, n_file_id in n_tree.files.items():
                if rel_path is not None:
                    file_path = os.path.join(rel_path, file_path)
                n_file = self.mvir.node(n_file_id)
                info = tarfile.TarInfo(file_path)
                info.size = len(n_file.body())
                t.addfile(info, io.BytesIO(n_file.body()))
        self._run_sudo(('tar', '-C', self.dir_path, '-x'), input=tar_io.getvalue())

    def checkout_file(self, rel_path, n_file):
        assert isinstance(n_file, FileNode)
        self.checkout_file_untracked(rel_path, n_file.body())

    def checkout_file_untracked(self, rel_path, body):
        assert not os.path.isabs(rel_path)
        assert isinstance(body, bytes)
        file_path = self.join(rel_path)
        cmd = 'mkdir -p {parent_path} && exec cat >{file_path}'.format(
            parent_path=shlex.quote(os.path.dirname(file_path)),
            file_path=shlex.quote(file_path),
        )
        self._run_sudo(('sh', '-c', cmd), input=body)

    def commit_dir(
        self,
        rel_path,
        ignore_spec: PathSpec | None = None,
        path_filter: Callable[[str], bool] | None = None,
    ):
        assert not os.path.isabs(rel_path)
        p = self._run_sudo(('tar', '-C', self.join(rel_path), '-c', '.'), stdout=subprocess.PIPE)
        tar_bytes = p.stdout
        tar_io = io.BytesIO(tar_bytes)
        files = {}
        with tarfile.open(fileobj=tar_io, mode='r') as t:
            while (info := t.next()) is not None:
                # Prefix output paths with the requested `rel_path`.
                dest_path = os.path.normpath(os.path.join(rel_path, info.name))
                if ignore_spec is not None and ignore_spec.match_file(dest_path):
                    continue
                if path_filter is not None and not path_filter(dest_path):
                    continue

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

                assert dest_path not in files, 'duplicate entry for %s' % dest_path
                f = t.extractfile(info)
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

        # `exec`, so that the signals `sudo` relays reach the command itself,
        # not just the shell.
        cmd = 'cd {dir_path} && exec {cmd}'.format(
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

        p.stdout.close()
        p.wait()
        self._forget(p)

        logs = bytes(acc)

        return p.returncode, logs


KEEP_TEMP_DIR = False
# Seconds `stop` waits for commands to exit on SIGTERM.
STOP_TIMEOUT = 10

@contextmanager
def run_sandbox(cfg, mvir, require_consistent_path = False):
    user = os.environ['CRISP_SANDBOX_SUDO_USER']

    dir_suffix = None
    if not require_consistent_path:
        dir_suffix = secrets.token_urlsafe(8)

    sb = SudoSandbox(mvir, user, dir_suffix = dir_suffix)
    sb.start()
    try:
        yield sb
    finally:
        sb.stop()

def set_keep_temp_dir(keep):
    global KEEP_TEMP_DIR
    KEEP_TEMP_DIR = keep
