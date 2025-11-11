import cbor
from functools import wraps
import inspect
import json
import os
import subprocess
import typing

from .config import Config
from .mvir import MVIR, NodeId, Node, TreeNode, TestResultNode, \
        CompileCommandsOpNode, FindUnsafeAnalysisNode
from .sandbox import Sandbox, run_sandbox


def _as_node_id(x):
    if isinstance(x, NodeId):
        return x
    else:
        return x.node_id()

def analysis(f):
    """
    Decorator for analysis functions, whose results are cached in the MVIR
    storage.

    Expected usage:
    ```
    @analysis
    def my_analysis(cfg: Config, mvir: MVIR, code: TreeNode, arg: str) -> MyAnalysisNode:
        ...
    ```

    Some arguments are handled specially:
    * The function should take an `MVIR` argument, which is used by the
      decorator.
    * Any `Config` or `Sandbox` arguments are ignored by the decorator.
    * The first `Node` or `NodeId` argument will be looked up in the MVIR index
      to find cached results.  This argument is otherwise treated normally.

    The remaining arguments must be a subset of the fields of the return type
    (which must be a `Node`).  When the decorated function is called, the
    decorator will look for an existing MVIR node whose field values match the
    arguments.  If a matching node exists, the decorator will return that node;
    otherwise, it will call the function to create one.
    """
    sig = inspect.signature(f)

    node_type = sig.return_annotation
    assert isinstance(node_type, type) and issubclass(node_type, Node), \
        'expected return type to be a Node subclass, but got %r' % node_type
    node_fields = typing.get_type_hints(node_type)

    mvir_param_name = None
    index_param_name = None
    match_fields = set()
    for param in sig.parameters.values():
        param_type = param.annotation
        if param_type in (Config, Sandbox):
            continue
        if param_type is MVIR:
            mvir_param_name = param.name
            continue
        if param.name not in node_fields:
            raise AttributeError('argument name %r does not match any field name in %r' %
                (param.name, node_type))
        if index_param_name is None:
            if param_type is NodeId \
                    or (isinstance(param_type, type) and issubclass(param_type, Node)):
                index_param_name = param.name
        match_fields.add(param.name)
    if mvir_param_name is None:
        raise AttributeError('no MVIR argument found in signature')
    if index_param_name is None:
        raise AttributeError('no Node or NodeId argument found in signature')

    @wraps(f)
    def g(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()

        def arg_matches(n, k):
            value = bound.arguments[k]
            if isinstance(value, Node):
                value = value.node_id()
            return value == getattr(n, k)

        mvir = bound.arguments[mvir_param_name]
        index_node_id = _as_node_id(bound.arguments[index_param_name])
        found = []
        for entry in mvir.index(index_node_id):
            if entry.kind != node_type.KIND or entry.key != index_param_name:
                continue
            candidate_n = mvir.node(entry.node_id)
            assert isinstance(candidate_n, node_type)
            if not all(arg_matches(candidate_n, k) for k in match_fields):
                continue
            found.append(candidate_n)

        if len(found) == 1:
            print('found %s' % found[0].node_id())
            return found[0]
        elif len(found) == 0:
            print('run %s' % (f,))
            n = f(*args, **kwargs)
            assert isinstance(n, node_type)
            for k in match_fields:
                assert arg_matches(n, k), \
                    'value mismatch on field %r: %r != %r' % (
                        k, getattr(n, k), bound.arguments[k])
            return n
        else:
            raise ValueError('found multiple index entries matching %r' %
                (bound.arguments,))
    return g

@analysis
def run_tests(cfg: Config, mvir: MVIR,
        code: TreeNode, test_code: TreeNode, cmd: str) -> TestResultNode:
    with run_sandbox(cfg, mvir) as sb:
        sb.checkout(code)
        sb.checkout(test_code)

        exit_code, logs = sb.run(cmd, shell=True, stream=True)

    n = TestResultNode.new(
            mvir,
            code = code.node_id(),
            test_code = test_code.node_id(),
            cmd = cmd,
            exit_code = exit_code,
            body = logs,
            )
    mvir.set_tag('test_results', n.node_id(), None)
    if n.passed:
        mvir.set_tag('test_passed', n.node_id(), None)
    return n

# We always check out the compile_commands.json at a consistent path, in case
# it contains relative paths.
COMPILE_COMMANDS_PATH = 'build/compile_commands.json'

@analysis
def _cc_cmake_impl(cfg: Config, mvir: MVIR, sb: Sandbox,
        c_code: TreeNode, cmd: list[str]) -> CompileCommandsOpNode:
    sb.checkout(c_code)
    exit_code, logs = sb.run(cmd)

    if exit_code == 0:
        n_cc = sb.commit_file(COMPILE_COMMANDS_PATH)
    else:
        n_cc = None
    n_cc_id = n_cc.node_id() if n_cc is not None else None

    n_op = CompileCommandsOpNode.new(
        mvir,
        body = logs,
        c_code = c_code.node_id(),
        cmd = cmd,
        exit_code = exit_code,
        compile_commands = n_cc_id,
        )
    return n_op

def cc_cmake(cfg: Config, mvir: MVIR, c_code: TreeNode) -> CompileCommandsOpNode:
    with run_sandbox(cfg, mvir) as sb:
        src_dir = sb.join(cfg.relative_path(cfg.transpile.cmake_src_dir))
        build_dir = sb.join(os.path.dirname(COMPILE_COMMANDS_PATH))
        cmd = ['cmake', '-B', build_dir, '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON', src_dir]
        n_op = _cc_cmake_impl(cfg, mvir, sb, c_code, cmd)

    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)
    if n_op.compile_commands is not None:
        mvir.set_tag('compile_commands', n_op.compile_commands, n_op.kind)
    return n_op

_CRISP_DIR = os.path.dirname(os.path.dirname(__file__))


def crisp_git_state(subdir=None) -> str:
    """
    Get a string representing the current state of the CRISP repo.  This is
    useful for ensuring that analysis tools get rerun when the tools change.
    If `subdir` is set, only the state of that subdirectory is considered.
    """
    if subdir is None:
        rev_cmd = ('git', 'rev-parse', 'HEAD')
    else:
        rev_cmd = ('git', 'rev-parse', 'HEAD:' + subdir)
    p = subprocess.run(rev_cmd, cwd=_CRISP_DIR, stdout=subprocess.PIPE)
    if p.returncode != 0:
        return 'unknown'
    rev = p.stdout.decode('utf-8').strip()

    status_cmd = ('git', 'status', '--porcelain=v1', '-z')
    if subdir is not None:
        status_cmd = status_cmd + (subdir,)
    p = subprocess.run(status_cmd, cwd=_CRISP_DIR, check=True, stdout=subprocess.PIPE)

    parts = p.stdout.split(b'\0')
    i = 0
    max_mtime = None
    had_error = False
    while i < len(parts):
        # Format of each line is usually `XY file.txt\0`, but renames and
        # copies have two filenames: `XY newfile.txt\0oldfile.txt\0`.
        part = parts[i]
        if len(part) == 0:
            i += 1
            continue
        assert part[2:3] == b' '
        name = part[3:].decode('utf-8')
        i += 1

        try:
            mtime = os.stat(os.path.join(_CRISP_DIR, name)).st_mtime
            if max_mtime is None or mtime > max_mtime:
                max_mtime = mtime
        except OSError as e:
            print('error checking mtime of %s: %s' % (name, e))
            had_error = True

        if part[0] in b'RC' or part[1] in b'RC':
            i += 1

    if max_mtime is not None:
        suffix = '-dirty-%d' % max_mtime
    elif had_error:
        suffix = '-dirty'
    else:
        suffix = ''

    return rev + suffix


@analysis
def _find_unsafe_impl(cfg: Config, mvir: MVIR,
        code: TreeNode, commit: str) -> FindUnsafeAnalysisNode:
    find_unsafe_dir = os.path.join(_CRISP_DIR, 'tools/find_unsafe')

    subprocess.run(('cargo', 'build', '--release'),
        cwd=find_unsafe_dir, check=True)

    input_cbor_bytes = cbor.dumps({
        name: mvir.node(node_id).body_str()
        for name, node_id in code.files.items()
        if name.endswith('.rs')
    })
    p = subprocess.run(('cargo', 'run', '--release'),
        cwd=find_unsafe_dir,
        input=input_cbor_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if p.returncode != 0:
        print('command failed with exit code %d' % p.returncode)
        print(' --- stdout ---\n%s\n' % p.stdout.decode('utf-8', errors='replace'))
        print(' --- stderr ---\n%s\n' % p.stderr.decode('utf-8', errors='replace'))
        p.check_returncode()

    # Check that stdout is valid json
    _ = json.loads(p.stdout.decode('utf-8'))

    n = FindUnsafeAnalysisNode.new(
            mvir,
            code = code.node_id(),
            commit = commit,
            stderr = p.stderr.decode('utf-8'),
            body = p.stdout,
            )
    return n

def find_unsafe(cfg: Config, mvir: MVIR, code: TreeNode) -> FindUnsafeAnalysisNode:
    commit = crisp_git_state('tools/find_unsafe')
    return _find_unsafe_impl(cfg, mvir, code, commit)
