import argparse
import glob
import json
import os
import requests
import shutil
import stat
import subprocess
import sys

from .config import Config
from .mvir import MVIR, NodeId, FileNode, LlmOpNode


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

def do_llm(cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    files = glob.glob(cfg.src_globs, root_dir=cfg.base_dir, recursive=True)
    assert len(files) == 1, 'expected exactly 1 src file, but got %r' % (files,)
    path = os.path.join(cfg.base_dir, files[0])

    orig_rust_code = open(path).read()
    n_old = FileNode.new(mvir, orig_rust_code.encode('utf-8'))
    mvir.set_tag('current', n_old, 'old')
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

    # Success - back up the previous version and overwrite with the new one.
    back_up_file(path)
    n_new = FileNode.new(mvir, code.encode('utf-8'))
    mvir.set_tag('current', n_new, 'new')
    open(path, 'w').write(code)

    n_op = LlmOpNode.new(
            mvir,
            old_code = n_old.node_id(),
            new_code = n_new.node_id(),
            raw_prompt = FileNode.new(mvir, LLM_PROMPT).node_id(),
            request = FileNode.new(mvir, json.dumps(req)).node_id(),
            response = FileNode.new(mvir, json.dumps(resp)).node_id(),
            )

    for x in mvir.tag_reflog('current'):
        print(x)

def do_test(cfg):
    try:
        subprocess.run(cfg.test_command, shell=True, check=True,
            cwd=cfg.base_dir)
        return True
    except subprocess.CalledProcessError as e:
        print('test command exited with code %d:\n```sh\n%s\n```' %
            (e.returncode, e.cmd.rstrip()))
        return False

def do_main(args, cfg):
    print(cfg)
    do_llm(cfg)
    do_test(cfg)

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
    else:
        raise ValueError('unknown command %r' % (args.cmd,))

if __name__ == '__main__':
    main()
