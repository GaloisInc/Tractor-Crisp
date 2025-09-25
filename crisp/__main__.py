import argparse
import glob
import json
import os
import pathlib
import requests
import stat
import subprocess
import sys
import tempfile

from . import analysis, llm
from .analysis import COMPILE_COMMANDS_PATH
from .config import Config
from .mvir import MVIR, NodeId, FileNode, TreeNode, LlmOpNode, \
    TestResultNode, CompileCommandsOpNode, TranspileOpNode
from .sandbox import run_sandbox
from .work_dir import lock_work_dir, set_keep_work_dir



def parse_args():
    ap = argparse.ArgumentParser()
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

    llm = sub.add_parser('llm')
    llm.add_argument('node', nargs='?', default='current')

    llm_repair = sub.add_parser('llm-repair')
    llm_repair.add_argument('node', nargs='?', default='current')
    llm_repair.add_argument('--c-code', default='c_code')

    test = sub.add_parser('test')
    test.add_argument('node', nargs='?', default='current')
    test.add_argument('--c-code', default='c_code')

    find_unsafe = sub.add_parser('find_unsafe')
    find_unsafe.add_argument('node', nargs='?', default='current')

    git = sub.add_parser('git')
    git.add_argument('-n', '--node', default='current')
    git.add_argument('args', nargs='*')

    return ap.parse_args()


def parse_node_id_arg(mvir, s):
    node_id, _ = parse_node_id_arg_and_check_tag(mvir, s)
    return node_id

def parse_node_id_arg_and_check_tag(mvir, s):
    """
    Parse `s` as a node ID.  Returns `(s, is_tag)`, where `is_tag` is `True` if
    `s` is a tag name.
    """
    if len(s) == 2 * NodeId.LENGTH:
        try:
            node_id = NodeId.from_str(s)
            return (node_id, False)
        except ValueError:
            pass
    if mvir.has_tag(s):
        return (mvir.tag(s), True)
    matches = mvir.node_ids_with_prefix(s)
    if len(matches) == 0:
        raise ValueError('node %r not found' % s)
    elif len(matches) == 1:
        return (matches[0], False)
    else:
        raise ValueError('found multiple nodes with prefix %r: %r' % (s, matches))


LLM_PROMPT = '''
This Rust code was auto-translated from C, so it is partly unsafe. Your task is to convert it to safe Rust, without changing its behavior. You must replace all unsafe operations (such as raw pointer dereferences and libc calls) with safe ones, so that you can remove unsafe blocks from the code and convert unsafe functions to safe ones. You may adjust types and data structures (such as replacing raw pointers with safe references) as needed to accomplish this.

HOWEVER, any function marked #[no_mangle] is an FFI entry point, which means its signature must not be changed. If such a function has unsafe types (such as raw pointers) in its signature and contains nontrivial logic, you should handle it as follows:
1. For a #[no_mangle] entry point named `foo`, make a new function `foo_impl` without the #[no_mangle] attribute.
2. Move all the logic from `foo` into `foo_impl`, and have `foo` simply be a wrapper that calls `foo_impl`.
3. If there are any calls to `foo` in the Rust code, change them to call `foo_impl` instead.
You can then make `foo_impl` safe like any other function, leaving `foo` as a simple unsafe wrapper for FFI callers.

After making the code safe, output the updated Rust code in a Markdown code block, with the file path on the preceding line, as shown in the input.

{input_files}
'''

def do_llm(args, cfg):
    '''Apply an LLM-based transformation to the codebase.  This takes the files
    identified by `cfg.src_globs`, passes them to the LLM, and overwrites the
    files with updated versions.  This creates a new `LlmOpNode` in the MVIR,
    which references the old and new states of the codebase, and records the
    node in the `op_history` reflog.'''
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    node_id, is_tag = parse_node_id_arg_and_check_tag(mvir, args.node)
    dest_tag = args.node if is_tag else 'current'
    n_tree = mvir.node(node_id)

    n_new_tree, n_op = llm.run_rewrite(cfg, mvir, LLM_PROMPT, n_tree,
        glob_filter = cfg.src_globs)

    mvir.set_tag(dest_tag, n_new_tree.node_id(), n_op.kind)
    print('new state: %s' % n_new_tree.node_id())
    print('operation: %s' % n_op.node_id())


LLM_REPAIR_PROMPT = '''
I tried compiling this Rust code and running the tests, but I got an error. Please fix the error so the code compiles and passes the tests. Try to avoid introducing any more unsafe code beyond what's already there.

Output the resulting Rust code in a Markdown code block, with the file path on the preceding line, as shown in the input.

{input_files}

Build/test logs:

```
{test_output}
```
'''

def do_llm_repair(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    node_id, is_tag = parse_node_id_arg_and_check_tag(mvir, args.node)
    dest_tag = args.node if is_tag else 'current'
    n_tree = mvir.node(node_id)

    c_code_node_id = parse_node_id_arg(mvir, args.c_code)
    n_c_code = mvir.node(c_code_node_id)

    n_test = analysis.run_tests(cfg, mvir, n_tree, n_c_code, cfg.test_command)

    n_new_tree, n_op = llm.run_rewrite(cfg, mvir, LLM_REPAIR_PROMPT, n_tree,
        glob_filter = cfg.src_globs,
        format_kwargs = {'test_output': n_test.body_str()},
        think=True)

    mvir.set_tag(dest_tag, n_new_tree.node_id(), n_op.kind)
    print('new state: %s' % n_new_tree.node_id())
    print('operation: %s' % n_op.node_id())


def do_cc_cmake(args, cfg):
    '''Generate compile_commands.json by running `cmake`.'''
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    node_id = parse_node_id_arg(mvir, args.node)
    node = mvir.node(node_id)

    n_op = analysis.cc_cmake(cfg, mvir, node)

    if n_op.exit_code != 0:
        print(n_op.body().decode('utf-8'))
    print('cmake process %s with code %d:\n%s' % (
        'succeeded' if n_op.exit_code == 0 else 'failed', n_op.exit_code, n_op.cmd))
    print('operation: %s' % n_op.node_id())
    print('result: %s' % n_op.compile_commands)

def transpile_common(cfg: Config, mvir: MVIR,
        n_cc: FileNode, n_c_code: TreeNode) -> TranspileOpNode:
    with run_sandbox(cfg, mvir) as sb:
        output_path = cfg.relative_path(cfg.transpile.output_dir)

        sb.checkout_file(COMPILE_COMMANDS_PATH, n_cc)
        sb.checkout(n_c_code)

        # Run c2rust-transpile
        c2rust_cmd = [
                'c2rust-transpile',
                sb.join(COMPILE_COMMANDS_PATH),
                '--output-dir', sb.join(output_path),
                '--emit-build-files',
                ]
        if cfg.transpile.bin_main is not None:
            c2rust_cmd.extend((
                '--binary', cfg.transpile.bin_main,
                ))
        exit_code, logs = sb.run(c2rust_cmd)

        if exit_code == 0:
            n_rust_code = sb.commit_dir(output_path)
        else:
            n_rust_code = None
        n_rust_code_id = n_rust_code.node_id() if n_rust_code is not None else None

    n_op = TranspileOpNode.new(
        mvir,
        body = logs,
        compile_commands = n_cc.node_id(),
        c_code = n_c_code.node_id(),
        cmd = c2rust_cmd,
        exit_code = exit_code,
        rust_code = n_rust_code_id,
        )
    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)
    if n_rust_code is not None:
        mvir.set_tag('current', n_rust_code.node_id(), n_op.kind)

    if exit_code != 0:
        # TODO: proper log parsing
        print(repr(logs))
    print('c2rust process %s with code %d:\n%s' % (
        'succeeded' if n_op.exit_code == 0 else 'failed', n_op.exit_code, n_op.cmd))

    return n_op

def do_transpile(args, cfg):
    '''Transpile from C to unsafe Rust.'''
    mvir = MVIR(cfg.mvir_storage_dir, '.')

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

    n_op = transpile_common(cfg, mvir, n_cc, n_c_code)

    print('operation: %s' % n_op.node_id())
    print('result: %s' % n_op.rust_code)

def do_test(args, cfg):
    """
    Run a test on the current codebase.  This produces a `TestResultNode` and
    adds it to the `test_results` reflog.  If the test succeeds, this also adds
    it to the `test_passed` reflog.
    """
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    node_id = parse_node_id_arg(mvir, args.node)
    n_code = mvir.node(node_id)

    c_code_node_id = parse_node_id_arg(mvir, args.c_code)
    n_c_code = mvir.node(c_code_node_id)

    n = analysis.run_tests(cfg, mvir, n_code, n_c_code, cfg.test_command)

    print('\ntest process %s with code %d:\n%s' % (
        'passed' if n.passed else 'failed', n.exit_code, n.cmd))
    print('result: %s' % n.node_id())

def do_find_unsafe(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    node_id = parse_node_id_arg(mvir, args.node)
    n_code = mvir.node(node_id)

    n = analysis.find_unsafe(cfg, mvir, n_code)

    json.dump(n.body_json(), sys.stdout, indent='  ')

    print('\nresult: %s' % n.node_id())

def count_unsafe(cfg: Config, mvir: MVIR, n_code: TreeNode) -> int:
    n_find_unsafe = analysis.find_unsafe(cfg, mvir, n_code)
    j_unsafe = n_find_unsafe.body_json()
    unsafe_count = sum(
        len(file_info['internal_unsafe_fns']) + len(file_info['fns_containing_unsafe'])
        for file_info in j_unsafe.values())
    return unsafe_count

def do_main(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    c_code_node_id = parse_node_id_arg(mvir, args.node)
    n_c_code = mvir.node(c_code_node_id)

    print(' ** cc_cmake')
    n_op_cc = analysis.cc_cmake(cfg, mvir, n_c_code)
    n_cc = mvir.node(n_op_cc.compile_commands)
    print(' ** transpile')
    n_op_transpile = transpile_common(cfg, mvir, n_cc, n_c_code)
    n_code = mvir.node(n_op_transpile.rust_code)
    print('n_code = %s' % n_code.node_id())
    print(' ** run_tests')
    n_op_test = analysis.run_tests(cfg, mvir, n_code, n_c_code, cfg.test_command)
    if n_op_test.exit_code != 0:
        print('error: test exit code after transpile = %d' % n_op_test.exit_code)
        return

    for safety_try in range(3):
        print(' ** count_unsafe')
        unsafe_count = count_unsafe(cfg, mvir, n_code)
        print('\n\niteration %d: %d unsafe functions remaining' %
              (safety_try + 1, unsafe_count))
        if unsafe_count == 0:
            break
        print('code = %s' % n_code.node_id())

        print(' ** llm (safety)')
        n_new_code, n_op_llm = llm.run_rewrite(
                cfg, mvir, LLM_PROMPT, n_code, glob_filter = cfg.src_globs)

        for repair_try in range(3):
            print(' ** run_tests')
            n_op_test = analysis.run_tests(cfg, mvir, n_new_code, n_c_code, cfg.test_command)
            print('\n  repair iteration %d: test exit code = %d' %
                  (repair_try + 1, n_op_test.exit_code))
            if n_op_test.passed:
                n_code = n_new_code
                break

            print(' ** llm (repair)')
            n_new_code, n_op_llm = llm.run_rewrite(cfg, mvir, LLM_REPAIR_PROMPT, n_new_code,
                glob_filter = cfg.src_globs,
                format_kwargs = {'test_output': n_op_test.body_str()},
                think=True)

    print('\n\n')
    print('final code = %s' % n_code.node_id())
    print('final c code = %s' % n_c_code.node_id())
    print(' ** run_tests')
    n_op_test = analysis.run_tests(cfg, mvir, n_code, n_c_code, cfg.test_command)
    print(' ** count_unsafe')
    unsafe_count = count_unsafe(cfg, mvir, n_code)
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
    elif args.cmd == 'llm':
        do_llm(args, cfg)
    elif args.cmd == 'llm-repair':
        do_llm_repair(args, cfg)
    elif args.cmd == 'test':
        do_test(args, cfg)
    elif args.cmd == 'find_unsafe':
        do_find_unsafe(args, cfg)
    elif args.cmd == 'git':
        do_git(args, cfg)
    else:
        raise ValueError('unknown command %r' % (args.cmd,))

if __name__ == '__main__':
    main()
