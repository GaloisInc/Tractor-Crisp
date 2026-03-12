from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import Path
import shlex
import subprocess
import toml
from subprocess import CompletedProcess, Popen

from ..config import ConfigBase
from ..mvir import FileNode, TreeNode
from ..work_dir import lock_work_dir
from ..util import ChunkPrinter


class DirMode(str, Enum):
    # Expose the host directory to the sandbox (read-only).
    EXPOSE = 'expose'
    # Create a directory in a different location and provide it to the sandbox
    # (read-write).  For example, this allows the sandbox to have its own
    # separate `~/.cargo` directory so that it can't see your Cargo
    # credentials.
    SANDBOX = 'sandbox'

    # Expose and add to `$PATH`.
    EXPOSE_PATH = 'expose_path'

@dataclass(frozen = True)
class BwrapConfig(ConfigBase):
    # Directories to provide inside the sandbox.  `/home` is normally hidden to
    # prevent the sandboxed code from exfiltrating sensitive data like
    # `~/.ssh/id_*`.  If you have `c2rust` or other tools installed into
    # `$HOME`, you can expose the directories containing those tools to the
    # sandbox by setting `dirs."~/path/to/c2rust" = "expose"`.
    #
    # Paths are run through `expanduser()`, which expands `~`/`~user`, but otherwise must be absolute.
    dirs: dict[str, DirMode] = field(default_factory = dict)
    # Extra entries to add to `$PATH` within the sandbox.  This list is
    # prepended to the current `$PATH`.
    extra_search_path: list[str] = field(default_factory = list)

CRISP_DIR = Path(__file__).parent.parent.parent

# Path to the work dir, as seen from inside the sandbox.
WORK_DIR_INSIDE = Path('/root/work')

class BwrapSandbox:
    """
    Helper for managing a `bwrap`-based sandbox.  This uses `bwrap` to run
    commands with restricted filesystem access.

    This is only intended for use during development.  It's meant to allow
    running CRISP using tools directly out of the `tools/*/target/debug/` build
    directories, so that it's not necessary to rebuild the docker container
    after each change to the tools.  It also picks up tools like `c2rust` and
    `hayroll` from the host's `$PATH`.  The user is responsible for making sure
    they have the right versions of these tools installed.

    The user must write a `crisp-bwrap.toml` config file and put it in their
    `tractor-crisp` directory.  See `BwrapConfig` for the available options.
    The user is responsible for configuring this in a way that does not expose
    sensitive information to untrusted code.
    """
    def __init__(self, mvir, work_dir):
        self.mvir = mvir
        self.work_dir = work_dir

        self.bwrap_cfg = BwrapConfig.from_toml_file(CRISP_DIR / 'crisp-bwrap.toml')

    def checkout(self, n_tree: TreeNode):
        self.work_dir.checkout(n_tree)

    def checkout_file(self, rel_path, n_file: FileNode):
        self.work_dir.checkout_file(rel_path, n_file)

    def commit_dir(self, rel_path) -> TreeNode:
        return self.work_dir.commit_dir(rel_path)

    def commit_file(self, rel_path) -> FileNode:
        return self.work_dir.commit_file(rel_path)

    def join(self, *other: StrPath) -> str:
        return str(WORK_DIR_INSIDE.joinpath(*other))

    def run(self, cmd, shell=False, stream=False, cwd: str = ".") -> tuple[int, str | bytes]:
        if shell:
            assert isinstance(cmd, str)
            cmd = ['sh', '-c', cmd]
        else:
            assert not isinstance(cmd, str)

        bwrap_cmd = [
            'bwrap',
            '--unshare-all', '--share-net',
            '--new-session',
            # Make `/` and `/dev` visible
            '--ro-bind', '/', '/',
            '--dev-bind', '/dev', '/dev',
            # Hide `/home` by default
            '--tmpfs', '/home',
            # Mount a tmpfs over `/root` so we can create the subdirectory
            # `/root/work`.
            '--tmpfs', '/root',
            # c2rust tries to create temporary files in `/tmp`.
            '--tmpfs', '/tmp',
            # Make `self.work_dir` visible at `/root/work`.  This is the same
            # path used for the work directory in the Docker sandbox, which
            # means `analysis` results from one can be reused in the other.
            '--bind', self.work_dir.path, WORK_DIR_INSIDE,
        ]

        # Add more `bind` entries based on config settings.
        extra_search_path = self.bwrap_cfg.extra_search_path.copy()
        for path, mode in self.bwrap_cfg.dirs.items():
            path = CRISP_DIR / Path(path).expanduser()
            match mode:
                case DirMode.EXPOSE:
                    bwrap_cmd.extend((
                        '--ro-bind', path, path,
                    ))
                case DirMode.EXPOSE_PATH:
                    bwrap_cmd.extend((
                        '--ro-bind', path, path,
                    ))
                    extra_search_path.append(path)
                case DirMode.SANDBOX:
                    sandbox_dir = CRISP_DIR / 'bwrap-sandbox' / str(path).replace('/', '__')
                    sandbox_dir.mkdir(parents = True, exist_ok = True)
                    bwrap_cmd.extend((
                        '--bind', sandbox_dir, path,
                    ))
                case _:
                    raise ValueError(f'unknown DirMode {mode!r}')

        extra_search_path = [str(x) for x in extra_search_path]
        search_path = ':'.join(extra_search_path + [os.environ['PATH']])
        bwrap_cmd.extend((
            '--chdir', self.join(cwd),
            '--',
            'env', f'PATH={search_path}',
        ))
        bwrap_cmd.extend(cmd)

        bwrap_cmd = [str(x) for x in bwrap_cmd]

        if not stream:
            p = subprocess.run(bwrap_cmd, check=False,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            return p.returncode, p.stdout

        p = subprocess.Popen(bwrap_cmd,
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


@contextmanager
def run_sandbox(cfg, mvir):
    with lock_work_dir(cfg, mvir) as work_dir:
        sb = BwrapSandbox(mvir, work_dir)
        yield sb

def set_keep_work_dir(keep):
    # We don't track this flag here.  The user should instead call
    # `crisp.work_dir.set_keep_work_dir(keep)`, since this module uses
    # `crisp.work_dir` to manage its temporary directory.
    pass
