import argparse
import glob
import json
import os
import requests
import shutil
import stat
import subprocess
import sys
import tempfile

from .config import Config
from .mvir import MVIR, NodeId, FileNode, TreeNode, LlmOpNode, \
    TestResultNode, CompileCommandsOpNode



def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', '-c', dest='config_path', default='crisp.toml')
    ap.add_argument('--mvir-storage-dir')

    sub = ap.add_subparsers(dest='cmd')

    main = sub.add_parser('main')

    reflog = sub.add_parser('reflog')
    reflog.add_argument('tag', nargs='?', default='current')

    show = sub.add_parser('show')
    show.add_argument('node', nargs='?', default='current')
    show.add_argument('--raw', action='store_true')

    index = sub.add_parser('index')
    index.add_argument('node', nargs='?', default='current')

    commit = sub.add_parser('commit')
    commit.add_argument('--tag', '-t', default='current')
    commit.add_argument('path', nargs='*')

    checkout = sub.add_parser('checkout')
    checkout.add_argument('node', nargs='?', default='current')

    cc_cmake = sub.add_parser('cc_cmake')

    llm = sub.add_parser('llm')

    test = sub.add_parser('test')

    return ap.parse_args()


def back_up_file(path):
    dir_name, base_name = os.path.split(path)

    # Make a copy named by the time of the last modification.
    mtime = os.stat(path).st_mtime
    mtime_path = os.path.join(dir_name, '%s.%d' % (base_name, int(mtime)))
    assert not os.path.exists(mtime_path), 'backup path already exists: %s' % mtime_path
    print('make backup: %s' % mtime_path)
    shutil.copyfile(path, mtime_path)

    # If this is the first version of the file that we've seen, save a copy
    # with a `.orig` extension.
    orig_path = os.path.join(dir_name, base_name + '.orig')
    if not os.path.exists(orig_path):
        print('make backup: %s' % orig_path)
        shutil.copyfile(path, orig_path)

LLM_ENDPOINT = 'http://localhost:8080/v1/chat/completions'

LLM_PROMPT = '''
Here is a piece of unsafe Rust code produced by C2Rust. Your task is to convert it to safe Rust, without changing its behavior.

* `#[no_mangle]` functions are FFI entry points, so leave their signatures as is - don't change any argument or return types or try to make them safe. You should still modify their bodies to reduce the amount of unsafe code or to account for changes to other functions that they call.
* All other functions should be made safe by converting all raw pointers to safe references and removing the `unsafe` and `extern "C"` qualifiers.

Output the resulting Rust code in a Markdown code block.

```Rust
{orig_rust_code}
```
'''

def do_llm(args, cfg):
    '''Apply an LLM-based transformation to the codebase.  This takes the files
    identified by `cfg.src_globs`, passes them to the LLM, and overwrites the
    files with updated versions.  This creates a new `LlmOpNode` in the MVIR,
    which references the old and new states of the codebase, and records the
    node in the `op_history` reflog.'''
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    n_tree = commit_node(mvir, cfg)
    assert isinstance(n_tree, TreeNode)
    assert len(n_tree.files) == 1, 'multi-file projects are not yet supported'

    (name, n_file_id), = list(n_tree.files.items())
    n_file = mvir.node(n_file_id)
    path = os.path.join(cfg.base_dir, name)

    orig_rust_code = n_file.body().decode('utf-8')
    prompt = LLM_PROMPT.format(orig_rust_code=orig_rust_code)

    print(prompt)
    print('send request...')
    req = {
        'messages': [
            {'role': 'user', 'content': prompt},
            {'role': 'assistant', 'content': '<think>\n</think>\n'},
        ],
    }
    resp = requests.post(LLM_ENDPOINT, json=req).json()

    print(resp)

    output = resp['choices'][0]['message']['content']
    print(' === output ===')
    print(output)
    print(' === end of output ===')
    # Extract the part delimited by ```Rust ... ```
    output = '\n%s\n' % output
    a, sep, b = output.rpartition('\n```\n')
    assert sep != ''
    c, sep, d = a.rpartition('\n```Rust\n')
    if sep == '':
        c, sep, d = a.rpartition('\n```rust\n')
    assert sep != ''
    code = d
    print(d)

    # Success - save the new version.
    n_new_file = FileNode.new(mvir, code.encode('utf-8'))
    n_new_tree = TreeNode.new(mvir, files={name: n_new_file.node_id()})
    open(path, 'wb').write(n_new_file.body())

    n_op = LlmOpNode.new(
            mvir,
            old_code = n_tree.node_id(),
            new_code = n_new_tree.node_id(),
            raw_prompt = FileNode.new(mvir, LLM_PROMPT).node_id(),
            request = FileNode.new(mvir, json.dumps(req)).node_id(),
            response = FileNode.new(mvir, json.dumps(resp)).node_id(),
            )
    # Record operations and timestamps in the `op_history` reflog.
    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)

    print('new state: %s' % n_new_tree.node_id())
    print('operation: %s' % n_op.node_id())

def do_cc_cmake(args, cfg):
    '''Generate compile_commands.json by running `cmake`.'''
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    src_dir = cfg.transpile.cmake_src_dir
    with tempfile.TemporaryDirectory(prefix='build.', dir=src_dir) as build_dir:
        cmake_cmd = ['cmake', '-B', build_dir, '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON', src_dir]
        p = subprocess.run(cmake_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if p.returncode == 0:
            with open(os.path.join(build_dir, 'compile_commands.json')) as f:
                n_cc = FileNode.new(mvir, f.read())
        else:
            n_cc = None
        n_op = CompileCommandsOpNode.new(
            mvir,
            body = p.stdout,
            cmd = cmake_cmd,
            exit_code = p.returncode,
            compile_commands = n_cc.node_id() if n_cc is not None else None,
            )
        mvir.set_tag('op_history', n_op.node_id(), n_op.kind)

    print('cmake process %s with code %d:\n%s' % (
        'succeeded' if n_op.exit_code == 0 else 'failed', n_op.exit_code, n_op.cmd))
    print('result: %s' % n_op.node_id())

def do_test(args, cfg):
    '''Run a test on the current codebase.  This produces a `TestResultNode`
    and adds it to the `test_results` reflog.  If the test succeeds, this also
    adds it to the `test_passed` reflog.'''
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    n_code = commit_node(mvir, cfg)
    p = subprocess.run(cfg.test_command, shell=True, cwd=cfg.base_dir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    n = TestResultNode.new(
            mvir,
            code = n_code.node_id(),
            cmd = cfg.test_command,
            exit_code = p.returncode,
            body = p.stdout,
            )
    mvir.set_tag('test_results', n.node_id(), None)
    if n.passed:
        mvir.set_tag('test_passed', n.node_id(), None)
    print(n.body().decode('utf-8'))
    print('\ntest process %s with code %d:\n%s' % (
        'passed' if n.passed else 'failed', n.exit_code, n.cmd))
    print('result: %s' % n.node_id())

def do_main(args, cfg):
    print(cfg)
    do_llm(args, cfg)
    do_test(args, cfg)

def do_reflog(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    for x in mvir.tag_reflog(args.tag):
        print(x)

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
    print('---')
    print(n.body().decode('utf-8'))

def get_src_paths(cfg):
    files = glob.glob(cfg.src_globs, root_dir=cfg.base_dir, recursive=True)
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
        assert os.path.commonpath((base, abs_path)) == base, \
                'path %r is outside project base directory %r' % (abs_path, base)
        rel_path = os.path.relpath(abs_path, base)
        assert not rel_path.startswith(os.pardir + os.sep)
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

    mvir.set_tag(args.tag, n.node_id())
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

    cfg_kwargs = {}
    if args.mvir_storage_dir is not None:
        cfg_kwargs['mvir_storage_dir'] = os.path.abspath(args.mvir_storage_dir)
    cfg = Config.from_toml_file(args.config_path, **cfg_kwargs)

    if args.cmd == 'main':
        do_main(args, cfg)
    elif args.cmd == 'reflog':
        do_reflog(args, cfg)
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
    elif args.cmd == 'llm':
        do_llm(args, cfg)
    elif args.cmd == 'test':
        do_test(args, cfg)
    else:
        raise ValueError('unknown command %r' % (args.cmd,))

if __name__ == '__main__':
    main()
