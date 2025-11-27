#!/usr/bin/env -S uv run

import argparse
import json
import os
import sys
import subprocess
import tempfile
import toml

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('project_dir')
    return ap.parse_args()

def get_target_info(project_dir):
    with tempfile.TemporaryDirectory() as td:
        query_file = os.path.join(td, '.cmake/api/v1/query/codemodel-v2')
        os.makedirs(os.path.dirname(query_file))
        with open(query_file, 'w') as f:
            pass

        subprocess.run(
                ('cmake', os.path.join(project_dir, 'test_case/CMakeLists.txt')),
                cwd=td,
                check=True,
                )

        reply_dir = os.path.join(td, '.cmake/api/v1/reply')

        index_jsons = []
        for f in os.listdir(reply_dir):
            if not (f.startswith('index-') and f.endswith('.json')):
                continue
            index_jsons.append(f)
        assert len(index_jsons) == 1, 'got multiple index.json files: %r' % (index_jsons,)
        index_json = index_jsons[0]

        with open(os.path.join(reply_dir, index_json)) as f:
            j_index = json.load(f)

        codemodel_json = j_index['reply']['codemodel-v2']['jsonFile']

        with open(os.path.join(reply_dir, codemodel_json)) as f:
            j_cm = json.load(f)

        target_jsons = []
        for j_cfg in j_cm['configurations']:
            for j_target in j_cfg['targets']:
                target_jsons.append(j_target['jsonFile'])
        # Expect one build target per project for now.
        #assert len(target_jsons) == 1, 'got multiple build targets: %r' % (target_jsons,)
        target_json = target_jsons[0]

        with open(os.path.join(reply_dir, target_json)) as f:
            j_target = json.load(f)

        return j_target

def find_git_root(path):
    orig_path = path
    while True:
        # Use `isdir` instead of `exists` so that the `gitdir: ...` files
        # placed in submodule roots will be ignored.
        if os.path.isdir(os.path.join(path, '.git')):
            return path
        new_path = os.path.dirname(path)
        assert new_path != path, 'found no .git directory above %r' % (orig_path,)
        path = new_path

def run_crisp(cli_args, *args, **kwargs):
    if 'cwd' not in kwargs:
        kwargs['cwd'] = cli_args.project_dir
    if 'check' not in kwargs:
        kwargs['check'] = True

    if 'env' not in kwargs:
        kwargs['env'] = os.environ.copy()
    env = kwargs['env']
    crisp_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    if 'PYTHONPATH' not in 'env':
        env['PYTHONPATH'] = crisp_dir
    else:
        env['PYTHONPATH'] += ':' + crisp_dir

    python3_bin = os.path.join(crisp_dir, '.venv', 'bin', 'python3')
    cmd = (python3_bin, '-m', 'crisp') + args

    return subprocess.run(cmd, **kwargs)

LIB_CONFIG_STR = r'''
base_dir = "{base_dir}"
project_name = "{example_name}"
src_globs = "translated_rust/src/*.rs"
test_command = """
set -e
export PYTHONPATH=$PWD/deployment/scripts/github-actions
cd {example_dir}
# Run non-Rust tests first so the C .so will be available for the Rust tests
python3 -m runtests --root {base_dir} -s {example_dir}
python3 -m runtests --root {base_dir} -s {example_dir} --rust --verbose
"""

[transpile]
cmake_src_dir = "test_case"
output_dir = "translated_rust"
single_target = "{target_filename}"
'''

BIN_CONFIG_STR = r'''
base_dir = "{base_dir}"
project_name = "{example_name}"
src_globs = "translated_rust/src/*.rs"
test_command = """
set -e
export PYTHONPATH=$PWD/deployment/scripts/github-actions
cd {example_dir}
# Run non-Rust tests first so the C .so will be available for the Rust tests
python3 -m runtests --root {base_dir} -s {example_dir}
python3 -m runtests --root {base_dir} -s {example_dir} --rust --verbose
"""

[transpile]
cmake_src_dir = "test_case"
output_dir = "translated_rust"
bin_main = "main"
single_target = "{target_filename}"
'''

def main():
    args = parse_args()

    # Extract CMake target info
    target_info = get_target_info(args.project_dir)

    # Write crisp.toml
    base_dir = find_git_root(args.project_dir)
    example_dir_rel = os.path.relpath(args.project_dir, base_dir)

    match target_info['type']:
        case 'STATIC_LIBRARY' | 'SHARED_LIBRARY':
            cfg_template = LIB_CONFIG_STR
        case 'EXECUTABLE':
            cfg_template = BIN_CONFIG_STR
        case t:
            raise ValueError('unknown CMake target type %r' % (t,))

    cfg_str = cfg_template.format(
            base_dir = os.path.relpath(base_dir, args.project_dir),
            example_dir = example_dir_rel,
            example_name = target_info['name'],
            target_filename = target_info['nameOnDisk'],
            )
    with open(os.path.join(args.project_dir, 'crisp.toml'), 'w') as f:
        f.write(cfg_str)

    # Collect source files
    src_files = []
    commit_files = [
            os.path.join(base_dir, 'Cargo.toml'),
            ]
    commit_dirs = [
            os.path.join(args.project_dir, 'runner'),
            os.path.join(args.project_dir, 'test_case'),
            os.path.join(args.project_dir, 'test_vectors'),
            os.path.join(base_dir, 'deployment'),
            os.path.join(base_dir, 'tools'),
            ]
    for path in commit_files:
        rel_path = os.path.relpath(path, args.project_dir)
        src_files.append(rel_path)
    for start_dir in commit_dirs:
        for root, dirs, files in os.walk(start_dir):
            for f in files:
                path = os.path.join(root, f)
                rel_path = os.path.relpath(path, args.project_dir)
                src_files.append(rel_path)
            for i in reversed(range(len(dirs))):
                if dirs[i] in ('target', '__pycache__'):
                    del dirs[i]

    run_crisp(args, 'commit', '-t', 'c_code', *src_files)
    run_crisp(args, 'main')

if __name__ == '__main__':
    main()
