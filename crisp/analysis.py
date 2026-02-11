import cbor
from functools import wraps
import inspect
import json
import os
import subprocess
import typing

from . import inline_errors as inline_errors_module
from .config import Config
from .mvir import (
    MVIR, NodeId, Node, FileNode, TreeNode, TestResultNode,
    CompileCommandsOpNode, FindUnsafeAnalysisNode, CargoCheckJsonAnalysisNode,
    InlineErrorsOpNode, DefNode, CrateNode, SplitOpNode, MergeOpNode,
)
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

@analysis
def cargo_check_json(cfg: Config, mvir: MVIR, code: TreeNode) -> CargoCheckJsonAnalysisNode:
    """
    Run `cargo check --message-format json` and capture the JSONL output.
    """

    rust_path_rel = cfg.relative_path(cfg.transpile.output_dir)

    with run_sandbox(cfg, mvir) as sb:
        sb.checkout(code)
        exit_code, logs = sb.run(
                'cd %s && cargo check --message-format=json' % rust_path_rel,
                shell=True, stream=True)

    j = []
    for line in logs.splitlines():
        if line.startswith(b'{'):
            # Print the human-readable version of the message for the logs
            j_line = json.loads(line)
            if isinstance(j_line, dict) and (j_msg := j_line.get('message')) is not None:
                if (j_rendered := j_msg.get('rendered')) is not None:
                    print(j_rendered, end='')
            j.append(j_line)
    n_json = FileNode.new(mvir, json.dumps(j))

    n_op = CargoCheckJsonAnalysisNode.new(
            mvir,
            code = code.node_id(),
            exit_code = exit_code,
            json = n_json.node_id(),
            body = logs,
            )
    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)
    return n_op

@analysis
def inline_errors(
    cfg: Config,
    mvir: MVIR,
    old_code: TreeNode,
    check_json: FileNode,
) -> InlineErrorsOpNode:
    """
    Take error messages from `check_json` and inline them into `old_code` as
    comments.
    """
    json_errors = check_json.body_json()

    errors_by_file, stderr_text = inline_errors_module.extract_diagnostics(json_errors)

    rust_path_rel = cfg.relative_path(cfg.transpile.output_dir)

    new_files = old_code.files.copy()
    for name, src_node_id in new_files.items():
        rel_name = os.path.relpath(name, rust_path_rel)
        if rel_name not in errors_by_file:
            continue
        errors = errors_by_file[rel_name]
        old_src = mvir.node(src_node_id).body_str()
        new_src = inline_errors_module.insert_inline_error_comments(
                old_src, errors, stderr_text)
        new_files[name] = FileNode.new(mvir, new_src).node_id()

    new_code = TreeNode.new(mvir, files = new_files)
    n_op = InlineErrorsOpNode.new(
            mvir,
            old_code = old_code.node_id(),
            new_code = new_code.node_id(),
            check_json = check_json.node_id(),
            )
    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)
    return n_op

# We always check out the compile_commands.json at a consistent path, in case
# it contains relative paths.
COMPILE_COMMANDS_PATH = "compile_commands.json"

@analysis
def _cc_cmake_impl(
    cfg: Config, mvir: MVIR, sb: Sandbox, c_code: TreeNode, cmds: list[list[str]]
) -> CompileCommandsOpNode:
    sb.checkout(c_code)

    exit_code = 0
    logs = []
    for cmd in cmds:
        if exit_code != 0:
            break
        exit_code, new_logs = sb.run(cmd)
        logs.append(new_logs)
    logs = b"\n\n".join(logs)

    if exit_code == 0:
        n_cc = sb.commit_file(COMPILE_COMMANDS_PATH)
    else:
        n_cc = None
    n_cc_id = n_cc.node_id() if n_cc is not None else None

    n_op = CompileCommandsOpNode.new(
        mvir,
        body=logs,
        c_code=c_code.node_id(),
        cmds=cmds,
        exit_code=exit_code,
        compile_commands=n_cc_id,
    )
    return n_op

def cc_cmake(cfg: Config, mvir: MVIR, c_code: TreeNode) -> CompileCommandsOpNode:
    with run_sandbox(cfg, mvir) as sb:
        src_dir = sb.join(cfg.relative_path(cfg.transpile.cmake_src_dir))
        build_dir = sb.join("build")
        config_cmd = ["cmake", "-B", build_dir, src_dir]
        build_cmd = ["bear", "--", "cmake", "--build", build_dir, "--"]
        if cfg.transpile.single_target is not None:
            build_cmd.append(cfg.transpile.single_target)
        cmds = [config_cmd, build_cmd]
        n_op = _cc_cmake_impl(cfg, mvir, sb, c_code, cmds)

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

def find_unsafe(cfg: Config, mvir: MVIR, code: TreeNode) -> CompileCommandsOpNode:
    commit = crisp_git_state('tools/find_unsafe')
    return _find_unsafe_impl(cfg, mvir, code, commit)


@analysis
def _split_rust_impl(
    cfg: Config, mvir: MVIR, sb: Sandbox, code_in: TreeNode, cmd: list[str]
) -> SplitOpNode:
    sb.checkout(code_in)

    exit_code, logs = sb.run(cmd)

    if exit_code == 0:
        if isinstance(logs, bytes):
            logs = logs.decode('utf-8')

        json_out = sb.commit_file('out.json')
        j = json_out.body_json()
        assert isinstance(j, dict)
        defs = {}
        for def_id, def_str in j.items():
            defs[def_id] = DefNode.new(mvir, body = def_str).node_id()
        crate_out = CrateNode.new(mvir, defs = defs)
    else:
        assert False
        json_out = FileNode.new(mvir, b'')
        crate_out = CrateNode.new(mvir, defs = {})

    n_op = SplitOpNode.new(
        mvir,
        # Note that saving `logs` here duplicates the entire JSON/`CrateNode`
        # output in the successful case.
        body = logs,
        cmd = cmd,
        exit_code = exit_code,
        code_in = code_in.node_id(),
        json_out = json_out.node_id(),
        crate_out = crate_out.node_id(),
    )
    return n_op

def split_rust(cfg: Config, mvir: MVIR, n_code: TreeNode) -> SplitOpNode:
    with run_sandbox(cfg, mvir) as sb:
        cargo_dir = sb.join(cfg.relative_path(cfg.transpile.output_dir))
        root_file = os.path.join(cargo_dir, 'src/lib.rs')
        cmd = ['split_rust', root_file, '--output-path', sb.join("out.json")]
        n_op = _split_rust_impl(cfg, mvir, sb, n_code, cmd)

    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)
    return n_op

@analysis
def _merge_rust_impl(
    cfg: Config, mvir: MVIR, sb: Sandbox,
    code_in: TreeNode, crate_in: CrateNode, cmd: list[str]
) -> MergeOpNode:
    sb.checkout(code_in)

    j = {k: mvir.node(v).body_str() for k, v in crate_in.defs.items()}
    json_in = FileNode.new(mvir, json.dumps(j))
    sb.checkout_file('in.json', json_in)

    exit_code, logs = sb.run(cmd)

    if exit_code == 0:
        if isinstance(logs, bytes):
            logs = logs.decode('utf-8')

        _ = sb.run(['rm', '-f', 'in.json'])

        code_out = sb.commit_dir('.')
    else:
        assert False
        code_out = TreeNode.new(mvir, files = {})

    n_op = MergeOpNode.new(
        mvir,
        # Note that saving `logs` here duplicates the entire JSON/`CrateNode`
        # output in the successful case.
        body = logs,
        cmd = cmd,
        exit_code = exit_code,
        code_in = code_in.node_id(),
        crate_in = crate_in.node_id(),
        code_out = code_out.node_id(),
    )
    return n_op

def merge_rust(cfg: Config, mvir: MVIR, n_code: TreeNode, n_crate: CrateNode) -> MergeOpNode:
    with run_sandbox(cfg, mvir) as sb:
        cargo_dir = sb.join(cfg.relative_path(cfg.transpile.output_dir))
        root_file = os.path.join(cargo_dir, 'src/lib.rs')
        cmd = ['merge_rust', root_file, sb.join("in.json")]
        n_op = _merge_rust_impl(cfg, mvir, sb, n_code, n_crate, cmd)

    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)
    return n_op
