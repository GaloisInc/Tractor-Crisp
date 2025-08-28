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
from .work_container import run_work_container, set_keep_work_container
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

    return ap.parse_args()


LLM_PROMPT = '''
Here is a piece of unsafe Rust code produced by C2Rust. Your task is to convert it to safe Rust, without changing its behavior.

* `#[no_mangle]` functions are FFI entry points, so leave their signatures as is - don't change any argument or return types or try to make them safe. You should still modify their bodies to reduce the amount of unsafe code or to account for changes to other functions that they call.
* All other functions should be made safe by converting all raw pointers to safe references and removing the `unsafe` and `extern "C"` qualifiers.

Output the resulting Rust code in a Markdown code block, with the file path on the preceding line, as shown in the input.

{input_files}
'''

def do_llm(args, cfg):
    '''Apply an LLM-based transformation to the codebase.  This takes the files
    identified by `cfg.src_globs`, passes them to the LLM, and overwrites the
    files with updated versions.  This creates a new `LlmOpNode` in the MVIR,
    which references the old and new states of the codebase, and records the
    node in the `op_history` reflog.'''
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    try:
        node_id = NodeId.from_str(args.node)
        dest_tag = 'current'
    except ValueError:
        node_id = mvir.tag(args.node)
        dest_tag = args.node
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

    try:
        node_id = NodeId.from_str(args.node)
        dest_tag = 'current'
    except ValueError:
        node_id = mvir.tag(args.node)
        dest_tag = args.node
    n_tree = mvir.node(node_id)

    try:
        c_code_node_id = NodeId.from_str(args.c_code)
    except ValueError:
        c_code_node_id = mvir.tag(args.c_code)
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

    try:
        node_id = NodeId.from_str(args.node)
    except ValueError:
        node_id = mvir.tag(args.node)
    node = mvir.node(node_id)

    n_op = analysis.cc_cmake(cfg, mvir, node)

    if n_op.exit_code != 0:
        print(n_op.body().decode('utf-8'))
    print('cmake process %s with code %d:\n%s' % (
        'succeeded' if n_op.exit_code == 0 else 'failed', n_op.exit_code, n_op.cmd))
    print('operation: %s' % n_op.node_id())
    print('result: %s' % n_op.compile_commands)

def do_transpile(args, cfg):
    '''Transpile from C to unsafe Rust.'''
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    try:
        cc_node_id = NodeId.from_str(args.compile_commands_node)
    except ValueError:
        cc_node_id = mvir.tag(args.compile_commands_node)
    n_cc = mvir.node(cc_node_id)

    if args.c_code_node is not None:
        try:
            c_code_node_id = NodeId.from_str(args.c_code_node)
        except ValueError:
            c_code_node_id = mvir.tag(args.c_code_node)
    else:
        for ie in mvir.index(n_cc.node_id()):
            if ie.kind == 'compile_commands_op' and ie.key == 'compile_commands':
                n_op = mvir.node(ie.node_id)
                c_code_node_id = n_op.c_code
                break
        else:
            raise ValueError("couldn't find a compile_commands_op for %s" % n_cc.node_id())
    n_c_code = mvir.node(c_code_node_id)

    with run_work_container(cfg, mvir) as wc:
        output_path = cfg.relative_path(cfg.transpile.output_dir)

        wc.checkout_file(COMPILE_COMMANDS_PATH, n_cc)
        wc.checkout(n_c_code)

        # Run c2rust-transpile
        c2rust_cmd = [
                'c2rust-transpile',
                wc.join(COMPILE_COMMANDS_PATH),
                '--output-dir', wc.join(output_path),
                '--emit-build-files',
                ]
        if cfg.transpile.bin_main is not None:
            c2rust_cmd.extend((
                '--binary', cfg.transpile.bin_main,
                ))
        exit_code, logs = wc.run(c2rust_cmd)

        if exit_code == 0:
            n_rust_code = wc.commit_dir(output_path)
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
    print('operation: %s' % n_op.node_id())
    print('result: %s' % n_rust_code_id)

def do_test(args, cfg):
    """
    Run a test on the current codebase.  This produces a `TestResultNode` and
    adds it to the `test_results` reflog.  If the test succeeds, this also adds
    it to the `test_passed` reflog.
    """
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    try:
        node_id = NodeId.from_str(args.node)
    except ValueError:
        node_id = mvir.tag(args.node)
    n_code = mvir.node(node_id)

    try:
        c_code_node_id = NodeId.from_str(args.c_code)
    except ValueError:
        c_code_node_id = mvir.tag(args.c_code)
    n_c_code = mvir.node(c_code_node_id)

    n = analysis.run_tests(cfg, mvir, n_code, n_c_code, cfg.test_command)

    print('\ntest process %s with code %d:\n%s' % (
        'passed' if n.passed else 'failed', n.exit_code, n.cmd))
    print('result: %s' % n.node_id())

def do_find_unsafe(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    try:
        node_id = NodeId.from_str(args.node)
    except ValueError:
        node_id = mvir.tag(args.node)
    n_code = mvir.node(node_id)

    n = analysis.find_unsafe(cfg, mvir, n_code)

    json.dump(n.body_json(), sys.stdout, indent='  ')

    print('\nresult: %s' % n.node_id())

def do_main(args, cfg):
    print(cfg)
    do_llm(args, cfg)
    do_test(args, cfg)

def do_reflog(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    for x in mvir.tag_reflog(args.tag):
        print(x)

def do_tag(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    try:
        node_id = NodeId.from_str(args.node)
    except ValueError:
        node_id = mvir.tag(args.node)
    mvir.set_tag(args.tag, node_id, 'tag')

def do_index(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    try:
        node_id = NodeId.from_str(args.node)
    except ValueError:
        node_id = mvir.tag(args.node)
    for x in mvir.index(node_id):
        print(x)

def do_show(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    try:
        node_id = NodeId.from_str(args.node)
    except ValueError:
        node_id = mvir.tag(args.node)
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

    try:
        node_id = NodeId.from_str(args.node)
    except ValueError:
        node_id = mvir.tag(args.node)
    new_n = mvir.node(node_id)
    if not isinstance(new_n, TreeNode):
        raise TypeError('expected TreeNode, but got %r' % (type(new_n),))

    # Commit the old code so it won't be lost
    old_n = commit_node(mvir, cfg)
    print('old state is %s' % old_n.node_id())

    # Remove old code
    for name, path in get_src_paths(cfg):
        os.unlink(path)

    # Create files matching the new state
    for name, file_node_id in new_n.files.items():
        file_n = mvir.node(file_node_id)
        path = os.path.join(cfg.base_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(file_n.body())

    print('checked out %s' % new_n.node_id())

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
    else:
        raise ValueError('unknown command %r' % (args.cmd,))

if __name__ == '__main__':
    main()
