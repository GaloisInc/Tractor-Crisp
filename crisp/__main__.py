import argparse
from contextlib import contextmanager
import glob
import json
import os
import requests
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Union, Sequence

from .config import Config
from .mvir import MVIR, NodeId, FileNode, TreeNode, LlmOpNode, \
    TestResultNode, CompileCommandsOpNode, TranspileOpNode



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
    cc_cmake.add_argument('node', nargs='?', default='c_code')

    transpile = sub.add_parser('transpile')
    transpile.add_argument('compile_commands_node', nargs='?', default='compile_commands')
    transpile.add_argument('c_code_node', nargs='?')

    llm = sub.add_parser('llm')

    test = sub.add_parser('test')

    return ap.parse_args()


class WorkDir:
    """
    Helper for manipulating the contents of a work directory.

    For operations that take an MVIR `TreeNode` as input and produce a new
    `TreeNode` as output by running a shell command, we copy the input files
    into a temporary work directory to reduce the chances that the command will
    be influenced by other untracked files in the main project directory.  This
    helps with reproducibility and also lets us create or modify files as
    needed without worrying about overwriting the user's data.

    The usual workflow with this type is to populate the directory with one or
    more inputs from MVIR using `checkout` methods, run some command on the
    inputs, and store the outputs back into MVIR using the `commit` methods.
    """
    def __init__(self, mvir, path):
        self.mvir = mvir
        self.path = path

    def checkout(self, n_tree):
        assert isinstance(n_tree, TreeNode)
        for rel_path, n_file_id in n_tree.files.items():
            n_file = self.mvir.node(n_file_id)
            self.checkout_file(rel_path, n_file)

    def checkout_file(self, rel_path, n_file):
        assert not os.path.isabs(rel_path)
        assert isinstance(n_file, FileNode)
        path = os.path.join(self.path, rel_path)
        assert not os.path.exists(path), \
            'path %r already exists in work dir %r' % (rel_path, self.path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(n_file.body())

    def commit(self, globs: Union[str, Sequence[str]]):
        if isinstance(globs, str):
            globs = (globs,)
        all_rel_paths = set(os.path.normpath(rel_path)
            for g in globs
            for rel_path in glob.glob(g, root_dir=self.path, recursive=True))
        dct = {}
        for rel_path in all_rel_paths:
            assert rel_path not in dct
            dct[rel_path] = self.commit_file(rel_path).node_id()
        return TreeNode.new(self.mvir, files=dct)

    def commit_file(self, rel_path):
        assert not os.path.isabs(rel_path)
        path = os.path.join(self.path, rel_path)
        assert os.path.exists(path)
        with open(path, 'rb') as f:
            return FileNode.new(self.mvir, f.read())

    def join(self, *args, **kwargs):
        return os.path.join(self.path, *args, **kwargs)

@contextmanager
def lock_work_dir(cfg, mvir):
    """
    Create a work directory based on `cfg`, and delete it on exit from the
    context manager.  This function raises an exception if the directory
    already exists.  As long as all processes follow this protocol, only one
    process can be inside the context manager at a time, so there's no risk of
    one process overwriting another process's files.
    """
    work_dir = os.path.join(cfg.mvir_storage_dir, 'work')
    # If the directory already exists, some other process holds the lock.
    os.makedirs(work_dir, exist_ok=False)
    try:
        yield WorkDir(mvir, work_dir)
    finally:
        shutil.rmtree(work_dir)


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

# We always check out the compile_commands.json at a consistent path, in case
# it contains relative paths.
COMPILE_COMMANDS_PATH = 'build/compile_commands.json'

def do_cc_cmake(args, cfg):
    '''Generate compile_commands.json by running `cmake`.'''
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    try:
        node_id = NodeId.from_str(args.node)
    except ValueError:
        node_id = mvir.tag(args.node)
    node = mvir.node(node_id)

    with lock_work_dir(cfg, mvir) as wd:
        src_dir = wd.join(cfg.relative_path(cfg.transpile.cmake_src_dir))
        build_dir = wd.join(os.path.dirname(COMPILE_COMMANDS_PATH))

        wd.checkout(node)

        cmake_cmd = ['cmake', '-B', build_dir, '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON', src_dir]
        p = subprocess.run(cmake_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        if p.returncode == 0:
            n_cc = wd.commit_file(COMPILE_COMMANDS_PATH)
        else:
            n_cc = None
        n_cc_id = n_cc.node_id() if n_cc is not None else None

    n_op = CompileCommandsOpNode.new(
        mvir,
        body = p.stdout,
        c_code = node.node_id(),
        cmd = cmake_cmd,
        exit_code = p.returncode,
        compile_commands = n_cc_id,
        )
    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)
    if n_cc is not None:
        mvir.set_tag('compile_commands', n_cc.node_id())

    if n_op.exit_code != 0:
        print(n_op.body().decode('utf-8'))
    print('cmake process %s with code %d:\n%s' % (
        'succeeded' if n_op.exit_code == 0 else 'failed', n_op.exit_code, n_op.cmd))
    print('operation: %s' % n_op.node_id())
    print('result: %s' % n_cc_id)

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

    with lock_work_dir(cfg, mvir) as wd:
        output_path = cfg.relative_path(cfg.transpile.output_dir)

        wd.checkout_file(COMPILE_COMMANDS_PATH, n_cc)
        wd.checkout(n_c_code)

        # Run c2rust-transpile
        c2rust_cmd = [
                'c2rust-transpile',
                wd.join(COMPILE_COMMANDS_PATH),
                '--output-dir', wd.join(output_path),
                '--emit-build-files',
                ]
        p = subprocess.run(c2rust_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        if p.returncode == 0:
            n_rust_code = wd.commit(os.path.join(output_path, '**/*.*'))
        else:
            n_rust_code = None
        n_rust_code_id = n_rust_code.node_id() if n_rust_code is not None else None

    n_op = TranspileOpNode.new(
        mvir,
        body = p.stdout,
        compile_commands = n_cc.node_id(),
        c_code = n_c_code.node_id(),
        cmd = c2rust_cmd,
        exit_code = p.returncode,
        rust_code = n_rust_code_id,
        )
    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)
    if n_rust_code is not None:
        mvir.set_tag('current', n_rust_code.node_id())

    if p.returncode != 0:
        print(p.stdout.decode('utf-8'))
    print('c2rust process %s with code %d:\n%s' % (
        'succeeded' if n_op.exit_code == 0 else 'failed', n_op.exit_code, n_op.cmd))
    print('operation: %s' % n_op.node_id())
    print('result: %s' % n_rust_code_id)

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
    elif args.cmd == 'transpile':
        do_transpile(args, cfg)
    elif args.cmd == 'llm':
        do_llm(args, cfg)
    elif args.cmd == 'test':
        do_test(args, cfg)
    else:
        raise ValueError('unknown command %r' % (args.cmd,))

if __name__ == '__main__':
    main()
