import argparse
import ast
import glob
import json
import os
import pathlib
import re
import requests
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from . import analysis, inline_errors, llm, sandbox
from .analysis import COMPILE_COMMANDS_PATH
from .config import Config
from .mvir import MVIR, NodeId, FileNode, TreeNode, LlmOpNode, \
    TestResultNode, CompileCommandsOpNode, TranspileOpNode, SplitFfiOpNode
from .sandbox import run_sandbox
from .work_dir import lock_work_dir, set_keep_work_dir
from .workflow import Workflow


ARG_PARSE_EPILOG = '''
In subcommand arguments, a `NODE` can be:
* A tag name, which refers to the most recent reflog entry for that tag
* A hexadecimal ID, or any unique prefix of one
* An expression `EXPR` that resolves to a node ID

An `EXPR` can be:
* Any `NODE`
* `NODE.foo`, which loads `NODE` and retrieves field `foo` from its metadata
* `EXPR[idx]`, which evaluates `EXPR` and then performs a Python indexing
  operation using `idx` (which must be a literal)

For example, if the `current` tag refers to a `TreeNode` containing a
`Cargo.toml` file, then `current.files["Cargo.toml"]` is a valid `NODE` that
refers to the `FileNode` for `Cargo.toml`.  `current.files` is an `EXPR` but
not a `NODE` because it evaluates to a dict rather than a node ID.
'''

def parse_args():
    ap = argparse.ArgumentParser(epilog=ARG_PARSE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', '-c', dest='config_path', default='crisp.toml')
    ap.add_argument('--mvir-storage-dir')
    ap.add_argument('--keep-work-dir', action='store_true',
        help='Preserve the `crisp-storage/work` temp directory.  '
            'Useful for debugging.  You must remove the directory manually '
            'before running further commands.')

    sub = ap.add_subparsers(dest='cmd')

    main = sub.add_parser('main')
    main.add_argument('node', nargs='?', default='c_code')

    reflog = sub.add_parser('reflog')
    reflog.add_argument('tag', nargs='?', default='current')

    tag = sub.add_parser('tag')
    tag.add_argument('--tag', '-t', default='current')
    tag.add_argument('node')

    show = sub.add_parser('show')
    show.add_argument('node', nargs='?', default='current')
    show.add_argument('--raw', action='store_true')
    show.add_argument('--files', action='store_true',
        help='If the target node is a TreeNode, show all files in the tree.')

    index = sub.add_parser('index')
    index.add_argument('node', nargs='?', default='current')

    commit = sub.add_parser('commit')
    commit.add_argument('--tag', '-t', default='current')
    commit.add_argument('path', nargs='*')

    checkout = sub.add_parser('checkout')
    checkout.add_argument('node', nargs='?', default='current')
    checkout.add_argument('--path', default='.',
        help='check out the files into this directory')

    cc_cmake = sub.add_parser('cc_cmake')
    cc_cmake.add_argument('node', nargs='?', default='c_code')

    transpile = sub.add_parser('transpile')
    transpile.add_argument('compile_commands_node', nargs='?', default='compile_commands')
    transpile.add_argument('c_code_node', nargs='?')

    split_ffi = sub.add_parser('split-ffi')
    split_ffi.add_argument('node', nargs='?', default='current')

    llm = sub.add_parser('llm')
    llm.add_argument('node', nargs='?', default='current')

    llm_repair = sub.add_parser('llm-repair')
    llm_repair.add_argument('node', nargs='?', default='current')
    llm_repair.add_argument('--c-code', default='c_code')

    test = sub.add_parser('test')
    test.add_argument('node', nargs='?', default='current')
    test.add_argument('--c-code', default='c_code')

    test = sub.add_parser('cargo-check-json')
    test.add_argument('node', nargs='?', default='current')

    test = sub.add_parser('inline-errors')
    test.add_argument('node', nargs='?', default='current')

    find_unsafe = sub.add_parser('find_unsafe')
    find_unsafe.add_argument('node', nargs='?', default='current')

    git = sub.add_parser('git')
    git.add_argument('-n', '--node', default='current')
    git.add_argument('args', nargs='*')

    return ap.parse_args()


def parse_node_id_arg(mvir, s):
    node_id, _ = parse_node_id_arg_and_check_tag(mvir, s)
    return node_id

HEX_DIGITS_RE = re.compile(r'[0-9a-fA-F]+')
OPERATOR_RE = re.compile(r'\[|\.')

def parse_node_id_expr(mvir: MVIR, node_str: str, expr_suffix: str) -> NodeId:
    """
    Parse a "ref expression" like `a1b2c3.foo` or `tag.bar[0]`.  `node_str`
    should be the base node, like `a1b2c3`, and `expr_suffix` should be the
    rest of the expression.

    The initial value is the `NodeId` obtained by parsing `node_str`.  The
    operations given in `expr_suffix` are then applied.  The supported
    operations are:
    - `.foo`: Take the current value, which must be a `NodeId`, load the
      `Node` with that ID, and look up attribute `foo` on it.
    - `[idx]`: Take the current value and access index `idx` on it.  `idx` can
      be any literal.  For example, `current.files["Cargo.toml"]` is a valid
      ref expression (assuming the tag `current` refers to a `TreeNode`).

    The final value must be a `NodeId`.
    """
    base_node_id = parse_node_id_arg(mvir, node_str)
    # `expr_suffix` will be something like `.foo`, `[0]`, or `.foo[0]`.  Add a
    # variable name to the front to make a complete expression.
    expr_ast = ast.parse('x' + expr_suffix, mode = 'eval')
    def go(a):
        match type(a):
            case ast.Name:
                return base_node_id
            case ast.Subscript:
                x = go(a.value)
                idx = ast.literal_eval(a.slice)
                return x[idx]
            case ast.Attribute:
                x = go(a.value)
                node = mvir.node(x)
                return getattr(node, a.attr)
            case _:
                raise TypeError(f'unsupported expression kind: {a}')
    final = go(expr_ast.body)
    assert isinstance(final, NodeId), \
            f'expected expr {node_str + expr_suffix!r} to produce a NodeId, but got {type(final)}'
    return final

def parse_node_id_arg_and_check_tag(mvir, s):
    """
    Parse `s` as a node ID.  Returns `(s, is_tag)`, where `is_tag` is `True` if
    `s` is a tag name.
    """
    # 1. Try parsing as a `NodeId`.
    if len(s) == 2 * NodeId.LENGTH:
        try:
            node_id = NodeId.from_str(s)
            return (node_id, False)
        except ValueError:
            pass
    # 2. Try parsing as a tag name.
    if mvir.has_tag(s):
        return (mvir.tag(s), True)

    # 3. Try parsing as an expression.
    m = OPERATOR_RE.search(s)
    if m is not None:
        i = m.start()
        return (parse_node_id_expr(mvir, s[:i], s[i:]), False)

    # 4. Try parsing as a prefix of a `NodeId`.
    matches = mvir.node_ids_with_prefix(s)
    if len(matches) == 0:
        raise ValueError('node %r not found' % s)
    elif len(matches) == 1:
        return (matches[0], False)
    else:
        raise ValueError('found multiple nodes with prefix %r: %r' % (s, matches))


def do_llm(args, cfg):
    '''Apply an LLM-based transformation to the codebase.  This takes the files
    identified by `cfg.src_globs`, passes them to the LLM, and overwrites the
    files with updated versions.  This creates a new `LlmOpNode` in the MVIR,
    which references the old and new states of the codebase, and records the
    node in the `op_history` reflog.'''
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir)

    node_id, is_tag = parse_node_id_arg_and_check_tag(mvir, args.node)
    dest_tag = args.node if is_tag else 'current'
    n_tree = mvir.node(node_id)

    n_new_tree, n_op = w.llm_safety_op(n_tree)

    mvir.set_tag(dest_tag, n_new_tree.node_id(), n_op.kind)
    print('new state: %s' % n_new_tree.node_id())
    print('operation: %s' % n_op.node_id())


def do_llm_repair(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir)

    node_id, is_tag = parse_node_id_arg_and_check_tag(mvir, args.node)
    dest_tag = args.node if is_tag else 'current'
    n_tree = mvir.node(node_id)

    c_code_node_id = parse_node_id_arg(mvir, args.c_code)
    n_c_code = mvir.node(c_code_node_id)

    n_test = w.test_op(n_tree, n_c_code)
    n_new_tree, n_op = w.llm_repair_op(n_tree, n_test)

    mvir.set_tag(dest_tag, n_new_tree.node_id(), n_op.kind)
    print('new state: %s' % n_new_tree.node_id())
    print('operation: %s' % n_op.node_id())


def do_cc_cmake(args, cfg):
    '''Generate compile_commands.json by running `cmake`.'''
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir)

    node_id = parse_node_id_arg(mvir, args.node)
    node = mvir.node(node_id)

    n_op = w.cc_cmake_op(node)

    if n_op.exit_code != 0:
        print(n_op.body().decode('utf-8'))
    print('cmake process %s with code %d:\n%s' % (
        'succeeded' if n_op.exit_code == 0 else 'failed', n_op.exit_code, n_op.cmd))
    print('operation: %s' % n_op.node_id())
    print('result: %s' % n_op.compile_commands)

def do_transpile(args, cfg):
    '''Transpile from C to unsafe Rust.'''
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir)

    cc_node_id = parse_node_id_arg(mvir, args.compile_commands_node)
    n_cc = mvir.node(cc_node_id)

    if args.c_code_node is not None:
        c_code_node_id = parse_node_id_arg(mvir, args.c_code_node)
    else:
        for ie in mvir.index(n_cc.node_id()):
            if ie.kind == 'compile_commands_op' and ie.key == 'compile_commands':
                n_op = mvir.node(ie.node_id)
                c_code_node_id = n_op.c_code
                break
        else:
            raise ValueError("couldn't find a compile_commands_op for %s" % n_cc.node_id())
    n_c_code = mvir.node(c_code_node_id)

    n_op = w.transpile_cc_op(n_c_code, n_cc)

    print('operation: %s' % n_op.node_id())
    print('result: %s' % n_op.rust_code)

def do_split_ffi(args, cfg):
    '''Run `split_ffi_entry_points` tool on Rust code.'''
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir)

    node_id, is_tag = parse_node_id_arg_and_check_tag(mvir, args.node)
    dest_tag = args.node if is_tag else 'current'
    n_tree = mvir.node(node_id)

    n_op = w.split_ffi_op(n_tree)

    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)
    mvir.set_tag(dest_tag, n_op.new_code, n_op.kind)
    print('new state: %s' % n_op.new_code)
    print('operation: %s' % n_op.node_id())

def do_test(args, cfg):
    """
    Run a test on the current codebase.  This produces a `TestResultNode` and
    adds it to the `test_results` reflog.  If the test succeeds, this also adds
    it to the `test_passed` reflog.
    """
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir)

    node_id = parse_node_id_arg(mvir, args.node)
    n_code = mvir.node(node_id)

    c_code_node_id = parse_node_id_arg(mvir, args.c_code)
    n_c_code = mvir.node(c_code_node_id)

    n = w.test_op(n_code, n_c_code)

    print('\ntest process %s with code %d:\n%s' % (
        'passed' if n.passed else 'failed', n.exit_code, n.cmd))
    print('result: %s' % n.node_id())

def do_cargo_check_json(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir)

    node_id = parse_node_id_arg(mvir, args.node)
    n_code = mvir.node(node_id)

    n = w.cargo_check_json_op(n_code)
    n_json = mvir.node(n.json)
    print(n_json.body_str())

    print('\ncargo check process %s with code %d' % (
        'passed' if n.passed else 'failed', n.exit_code))
    print('operation: %s' % n.node_id())
    print('json: %s' % n_json.node_id())

def do_inline_errors(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir)

    node_id = parse_node_id_arg(mvir, args.node)
    n_code = mvir.node(node_id)

    n_op = w.inline_errors_op(n_code)
    print('operation: %s' % n_op.node_id())
    print('new state: %s' % n_op.new_code)

def do_find_unsafe(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir)

    node_id = parse_node_id_arg(mvir, args.node)
    n_code = mvir.node(node_id)

    n = w.find_unsafe_op(n_code)

    json.dump(n.body_json(), sys.stdout, indent='  ')

    print('\nresult: %s' % n.node_id())

def do_main(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir)

    c_code_node_id = parse_node_id_arg(mvir, args.node)
    n_c_code = mvir.node(c_code_node_id)

    # Hacks to get the C path relative to `n_tree`.  This handles
    # tricks like `base_dir = ".."` used by the testing scripts.
    # TODO: clean up config path handling and get rid of this
    config_path = Path(cfg.config_path).parent.resolve()
    base_path = Path(cfg.base_dir).resolve()
    c_path = config_path / cfg.transpile.cmake_src_dir
    c_path_rel = c_path.relative_to(base_path)

    paths = (Path(path) for path in n_c_code.files.keys())
    num_c_files = sum(
        1 for path in paths if path.suffix == ".c" and path.is_relative_to(c_path_rel)
    )

    # Try transpiling with Hayroll first, then fall back to plain C2Rust.  Note
    # that `w.transpile` also checks that the tests pass, so a successful
    # transpile with failing tests counts as a failure here.
    n_code = None
    if n_code is None and num_c_files > 1:
        n_code = w.transpile(
            n_c_code,
            src_loc_annotations=True,
            refactor_transforms=("rename_unnamed", "reorganize_definitions"),
        )
    if n_code is None:
        n_code = w.transpile(n_c_code, src_loc_annotations=True, hayroll=True)
    if n_code is None:
        n_code = w.transpile(n_c_code)
    if n_code is None:
        return
    w.accept(n_code, ('main', 'transpile'))

    n_code = w.split_ffi(n_code)
    if not w.test(n_code, n_c_code):
        print('error: tests failed after split_ffi')
        return None
    w.accept(n_code, ('main', 'split_ffi'))

    llm_safety_tries = int(os.environ.get("LLM_SAFETY_TRIES", "3"))
    for safety_try in range(llm_safety_tries):
        unsafe_count = w.count_unsafe(n_code)
        if unsafe_count == 0:
            break

        n_new_code = w.llm_safety(n_code)

        for repair_try in range(3):
            n_op_check = w.cargo_check_json_op(n_new_code)
            if not n_op_check.passed:
                n_new_code = w.llm_repair_compile(n_new_code, n_op_check)

                n_op_check = w.cargo_check_json_op(n_new_code)
                if not n_op_check.passed:
                    # If we failed to fix the compile errors, don't bother
                    # trying to run tests.  This still counts as a repair
                    # attempt.
                    continue

            n_op_test = w.test_op(n_new_code, n_c_code)
            if n_op_test.exit_code == 0:
                w.accept(n_new_code, ('main', 'safety', safety_try))
                n_code = n_new_code
                break

            n_new_code = w.llm_repair(n_new_code, n_op_test)

    print('\n\n')
    print('final code = %s' % n_code.node_id())
    print('final c code = %s' % n_c_code.node_id())
    n_op_test = w.test_op(n_code, n_c_code)
    unsafe_count = w.count_unsafe(n_code)
    print('final unsafe count = %d' % unsafe_count)
    print('final test exit code = %d' % n_op_test.exit_code)

def do_reflog(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    for x in mvir.tag_reflog(args.tag):
        print(x)

def do_tag(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    node_id = parse_node_id_arg(mvir, args.node)
    mvir.set_tag(args.tag, node_id, 'tag')

def do_index(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    node_id = parse_node_id_arg(mvir, args.node)
    for x in mvir.index(node_id):
        print(x)

def do_show(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    node_id = parse_node_id_arg(mvir, args.node)
    print(node_id)
    n = mvir.node(node_id)
    from pprint import pprint
    if not args.raw:
        pprint(n.metadata())
    else:
        pprint(n.read_raw_metadata())
    if not args.files:
        print('---')
        print(n.body().decode('utf-8'))
    else:
        for name, file_node_id in n.files.items():
            print(' --- %s: ---' % name)
            print(mvir.node(file_node_id).body().decode('utf-8'))

def get_src_paths(cfg):
    files = set(f
        for g in cfg.src_globs
        for f in glob.glob(g, root_dir=cfg.base_dir, recursive=True))
    for name in files:
        path = os.path.join(cfg.base_dir, name)
        yield name, path

def commit_node(mvir, cfg):
    dct = {}
    for name, path in get_src_paths(cfg):
        with open(path, 'rb') as f:
            dct[name] = FileNode.new(mvir, f.read()).node_id()
    return TreeNode.new(mvir, files=dct)

def do_commit(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    base = os.path.abspath(cfg.base_dir)
    all_paths = {}
    for path in args.path:
        abs_path = os.path.abspath(path)
        rel_path = cfg.relative_path(abs_path)
        assert all_paths.get(rel_path, abs_path) == abs_path
        all_paths[rel_path] = abs_path

    dct = {}
    for rel_path, abs_path in all_paths.items():
        assert rel_path not in dct
        with open(abs_path, 'rb') as f:
            n_file = FileNode.new(mvir, f.read())
            print('%s: %s' % (rel_path, n_file.node_id()))
            dct[rel_path] = n_file.node_id()
    n = TreeNode.new(mvir, files=dct)

    mvir.set_tag(args.tag, n.node_id(), 'commit')
    print('committed %s = %s' % (args.tag, n.node_id()))

def do_checkout(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    node_id = parse_node_id_arg(mvir, args.node)
    new_n = mvir.node(node_id)
    if not isinstance(new_n, TreeNode):
        raise TypeError('expected TreeNode, but got %r' % (type(new_n),))

    # Create files matching the new state
    for name, file_node_id in new_n.files.items():
        file_n = mvir.node(file_node_id)
        path = os.path.join(args.path, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(file_n.body())

    print('checked out %s' % new_n.node_id())

def do_git(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    node_id = parse_node_id_arg(mvir, args.node)

    from . import git
    oid = git.render(mvir, mvir.node(node_id))
    env = os.environb.copy()
    env[b'GIT_DIR'] = os.fsencode(git.repo_path(mvir))

    # If the user writes `{}` anywhere in the git args, it will be replaced
    # with the generated git object ID.  Otherwise, the object ID will be
    # appended to the command.
    cmd = ['git'] + args.args
    replaced = False
    for i, arg in enumerate(cmd):
        if '{}' in arg:
            cmd[i] = arg.format(str(oid))
            replaced = True
    if not replaced:
        cmd.append(str(oid))

    os.execvpe('git', cmd, env)

def main():
    args = parse_args()

    set_keep_work_dir(args.keep_work_dir)
    sandbox.set_keep(args.keep_work_dir)

    cfg_kwargs = {}
    if args.mvir_storage_dir is not None:
        cfg_kwargs['mvir_storage_dir'] = os.path.abspath(args.mvir_storage_dir)
    cfg = Config.from_toml_file(args.config_path, **cfg_kwargs)

    if args.cmd == 'main':
        do_main(args, cfg)
    elif args.cmd == 'reflog':
        do_reflog(args, cfg)
    elif args.cmd == 'tag':
        do_tag(args, cfg)
    elif args.cmd == 'show':
        do_show(args, cfg)
    elif args.cmd == 'index':
        do_index(args, cfg)
    elif args.cmd == 'commit':
        do_commit(args, cfg)
    elif args.cmd == 'checkout':
        do_checkout(args, cfg)
    elif args.cmd == 'cc_cmake':
        do_cc_cmake(args, cfg)
    elif args.cmd == 'transpile':
        do_transpile(args, cfg)
    elif args.cmd == 'split-ffi':
        do_split_ffi(args, cfg)
    elif args.cmd == 'llm':
        do_llm(args, cfg)
    elif args.cmd == 'llm-repair':
        do_llm_repair(args, cfg)
    elif args.cmd == 'test':
        do_test(args, cfg)
    elif args.cmd == 'cargo-check-json':
        do_cargo_check_json(args, cfg)
    elif args.cmd == 'inline-errors':
        do_inline_errors(args, cfg)
    elif args.cmd == 'find_unsafe':
        do_find_unsafe(args, cfg)
    elif args.cmd == 'git':
        do_git(args, cfg)
    else:
        raise ValueError('unknown command %r' % (args.cmd,))

if __name__ == '__main__':
    main()
