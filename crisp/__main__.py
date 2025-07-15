import glob
import os
import requests
import shutil
import stat
import subprocess
import sys

from .config import Config
from .mvir import MVIR

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
    mvir = MVIR('crisp-storage', '.')

    files = glob.glob(cfg.src_globs, root_dir=cfg.base_dir, recursive=True)
    assert len(files) == 1, 'expected exactly 1 src file, but got %r' % (files,)
    path = os.path.join(cfg.base_dir, files[0])

    orig_rust_code = open(path).read()
    mvir.set_tag('current', mvir.new_node({}, orig_rust_code.encode('utf-8')), 'old')
    prompt = LLM_PROMPT.format(orig_rust_code=orig_rust_code)

    print(prompt)
    print('send request...')
    resp = requests.post(LLM_ENDPOINT, json={
        'messages': [
            {'role': 'user', 'content': prompt},
            {'role': 'assistant', 'content': '<think>\n</think>\n'},
        ],
    }).json()

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
    mvir.set_tag('current', mvir.new_node({}, code.encode('utf-8')), 'new')
    open(path, 'w').write(code)

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

def main():
    config_path, = sys.argv[1:]

    cfg = Config.from_toml_file(config_path)
    print(cfg)
    do_llm(cfg)
    do_test(cfg)

if __name__ == '__main__':
    main()
