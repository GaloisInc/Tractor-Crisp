#!/usr/bin/env python3
"""
Run CRISP-generated characterisation tests against the Rust translation.

`python -m crisp.gen_tests` records what each test does on the original C code.
This script replays the same tests against the Rust translation and compares
the results:

* `driver` mode: each test's `driver.c` is compiled and linked against the
  Rust crate's `cdylib`, so the driver's calls go through the translation's
  FFI entry points (`#[no_mangle] extern "C"` functions) instead of the C
  sources.
* `cli` mode: the Rust binary is run with each test's arguments and stdin.

In both modes, stdout and the exit code must match the `expected_stdout` and
`expected_exit_code` observed on the C code.

`gen_tests` ships a copy of this script as `run_tests.py` in the tests
directory, next to `manifest.json`.  It uses only the Python standard library,
so it runs anywhere the tests are checked out, including inside the CRISP
sandbox, e.g. as the project's `test_command`:

    python3 crisp_tests/run_tests.py

Exit status: 0 if all tests pass, 1 if any fail, 2 if the setup (building the
Rust code, finding the library) fails.
"""

import argparse
import difflib
import glob
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


RE_RECORD = re.compile(rb'^[!-~]+ = ')
# A record line holding a double, as printed by `obs_dbl`: the value always
# contains `.`, `e`, `inf` or `nan`, which tells it apart from integers.
RE_FLOAT_RECORD = re.compile(
    rb'^([!-~]+) = (-?(?:nan|inf|[0-9]*\.[0-9]*(?:e[-+]?[0-9]+)?|[0-9]+e[-+]?[0-9]+))$')
DIFF_LINES = 60
DEFAULT_FLOAT_REL_TOL = 1e-6


def parse_args(argv = None):
    ap = argparse.ArgumentParser(description = __doc__.strip().splitlines()[0])
    ap.add_argument('--tests-dir', default = os.path.dirname(os.path.abspath(__file__)),
        help = 'directory containing manifest.json (default: this script\'s directory)')
    ap.add_argument('--rust-dir',
        help = 'Rust workspace containing Cargo.toml, relative to the project root '
            '(default: from the manifest)')
    ap.add_argument('--no-build', action = 'store_false', dest = 'build',
        help = "don't run `cargo build` first")
    ap.add_argument('--release', action = 'store_true',
        help = 'build and use the release profile instead of debug')
    ap.add_argument('--lib', metavar = 'PATH_OR_NAME',
        help = 'driver mode: the cdylib to link against, as a path or a crate name '
            '(default: the artifact name from the manifest)')
    ap.add_argument('--run-cmd',
        help = 'cli mode: command that runs the Rust program, relative to the project '
            'directory (default: the artifact binary in the target directory)')
    ap.add_argument('--compare', choices = ('stdout', 'records'), default = 'stdout',
        help = '`stdout`: all of stdout must match.  `records`: only obs_* record lines '
            'must match, ignoring other output (driver mode).  (default: %(default)s)')
    ap.add_argument('--float-rel-tol', type = float, default = DEFAULT_FLOAT_REL_TOL,
        help = 'relative tolerance for obs_dbl values: |actual - expected| <= tol * '
            'max(|expected|, 1); NaN matches NaN (default: %(default)s)')
    ap.add_argument('--timeout', type = float, help = 'per-test time limit in seconds '
        '(default: from the manifest)')
    ap.add_argument('--only', action = 'append', metavar = 'NAME',
        help = 'run only this test (may be repeated)')
    ap.add_argument('--json-report', metavar = 'FILE', help = 'write results as JSON')
    ap.add_argument('--keep-work-dir', action = 'store_true',
        help = 'keep the temporary directory with built drivers')
    return ap.parse_args(argv)


class SetupError(Exception):
    pass


def read_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


def find_cdylib(target_dir, lib):
    """Find the shared library to link test drivers against."""
    if lib is not None and (os.sep in lib or lib.endswith('.so')):
        if not os.path.isfile(lib):
            raise SetupError(f'library {lib} not found')
        return os.path.abspath(lib)

    candidates = sorted(glob.glob(os.path.join(target_dir, 'lib*.so')))
    if lib is not None:
        want = os.path.join(target_dir, f'lib{lib.replace("-", "_")}.so')
        if want in candidates:
            return want
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise SetupError(f'no cdylib (lib*.so) found in {target_dir}; '
            'driver mode needs a library crate with crate-type = ["cdylib"]')
    raise SetupError(f'multiple cdylibs found in {target_dir}: '
        + ', '.join(os.path.basename(c) for c in candidates) + '; pick one with --lib')


def records_only(out: bytes) -> bytes:
    return b''.join(line + b'\n' for line in out.splitlines() if RE_RECORD.match(line))


def floats_close(e: float, a: float, rel_tol: float) -> bool:
    if math.isnan(e) or math.isnan(a):
        return math.isnan(e) and math.isnan(a)
    if math.isinf(e) or math.isinf(a):
        return a == e
    return a == e or abs(a - e) <= rel_tol * max(abs(e), 1.0)

def outputs_match(expected: bytes, actual: bytes, rel_tol: float) -> bool:
    """
    Compare outputs line by line.  Lines must be identical, except that
    `obs_dbl` records with the same label may differ within `rel_tol`.
    """
    if expected == actual:
        return True
    exp_lines, act_lines = expected.splitlines(), actual.splitlines()
    if len(exp_lines) != len(act_lines) or expected.endswith(b'\n') != actual.endswith(b'\n'):
        return False
    for e, a in zip(exp_lines, act_lines):
        if e == a:
            continue
        me, ma = RE_FLOAT_RECORD.match(e), RE_FLOAT_RECORD.match(a)
        if me is None or ma is None or me.group(1) != ma.group(1):
            return False
        if not floats_close(float(me.group(2)), float(ma.group(2)), rel_tol):
            return False
    return True


def diff(expected: bytes, actual: bytes) -> str:
    exp = expected.decode('utf-8', errors = 'replace').splitlines()
    act = actual.decode('utf-8', errors = 'replace').splitlines()
    lines = list(difflib.unified_diff(exp, act, 'expected', 'actual', lineterm = ''))
    if len(lines) > DIFF_LINES:
        lines = lines[:DIFF_LINES] + [f'... ({len(lines) - DIFF_LINES} more diff lines)']
    return '\n'.join(lines)


def tail(b: bytes, limit = 2000) -> str:
    s = b.decode('utf-8', errors = 'replace')
    return s if len(s) <= limit else '...\n' + s[-limit:]


def run_process(cmd, cwd, stdin_path, timeout, env = None, shell = False):
    """Returns `(exit_code, stdout, stderr)`; `exit_code` is `None` on timeout."""
    stdin = open(stdin_path, 'rb') if stdin_path else subprocess.DEVNULL
    try:
        p = subprocess.run(cmd, cwd = cwd, stdin = stdin, stdout = subprocess.PIPE,
            stderr = subprocess.PIPE, timeout = timeout, env = env, shell = shell)
    except subprocess.TimeoutExpired as e:
        return None, e.stdout or b'', e.stderr or b''
    finally:
        if stdin_path:
            stdin.close()
    code = p.returncode
    # Report signals the way a shell does, matching how the C run was recorded.
    if code < 0:
        code = 128 - code
    return code, p.stdout, p.stderr


def judge(code, out, err, expected_out, expected_code, compare, timeout,
        rel_tol = DEFAULT_FLOAT_REL_TOL):
    if code is None:
        return 'timeout', f'did not finish within {timeout} seconds'
    if code > 128:
        return 'crashed', f'killed by signal {code - 128}; stderr:\n{tail(err)}'
    exp, act = expected_out, out
    if compare == 'records':
        exp, act = records_only(exp), records_only(act)
    if not outputs_match(exp, act, rel_tol):
        return 'wrong_output', diff(exp, act)
    if code != expected_code:
        return 'wrong_exit_code', f'expected exit code {expected_code}, got {code}; ' \
            f'stderr:\n{tail(err)}'
    return 'pass', ''


def main(argv = None):
    args = parse_args(argv)
    tests_dir = os.path.abspath(args.tests_dir)
    with open(os.path.join(tests_dir, 'manifest.json')) as f:
        manifest = json.load(f)
    root = os.path.normpath(os.path.join(tests_dir, manifest.get('root_from_tests', '.')))
    mode = manifest['mode']
    timeout = args.timeout or manifest.get('timeout', 10)
    profile = 'release' if args.release else 'debug'

    rust_dir = args.rust_dir or manifest.get('rust_dir')
    if rust_dir is None:
        print('error: --rust-dir is required (the manifest does not record it)', file = sys.stderr)
        return 2
    rust_dir = os.path.join(root, rust_dir)
    target_dir = os.path.join(rust_dir, 'target', profile)

    tests = manifest['tests']
    if args.only:
        known = {t['name'] for t in tests}
        missing = sorted(set(args.only) - known)
        if missing:
            print(f'error: unknown tests: {", ".join(missing)}', file = sys.stderr)
            return 2
        tests = [t for t in tests if t['name'] in args.only]

    try:
        if args.build:
            cmd = ['cargo', 'build', '--manifest-path', os.path.join(rust_dir, 'Cargo.toml')]
            if args.release:
                cmd.append('--release')
            print(f'+ {shlex.join(cmd)}', flush = True)
            if subprocess.run(cmd, cwd = root).returncode != 0:
                raise SetupError('cargo build failed')

        if mode == 'driver':
            lib_path = find_cdylib(target_dir, args.lib or manifest.get('artifact'))
            print(f'linking test drivers against {lib_path}')
        else:
            run_cmd = args.run_cmd
            if run_cmd is None:
                artifact = manifest.get('artifact')
                if artifact is None:
                    raise SetupError('--run-cmd is required (the manifest does not record '
                        'the artifact name)')
                run_cmd = shlex.quote(os.path.join(target_dir, artifact))
            print(f'running tests with {run_cmd}')
    except SetupError as e:
        print(f'error: {e}', file = sys.stderr)
        return 2

    work_dir = tempfile.mkdtemp(prefix = 'crisp-gen-tests-')
    results = []
    try:
        for test in tests:
            name = test['name']
            test_dir = os.path.join(tests_dir, name)
            stdin_path = os.path.join(test_dir, 'stdin')
            stdin_path = stdin_path if os.path.exists(stdin_path) else None
            expected_out = read_bytes(os.path.join(test_dir, 'expected_stdout'))
            expected_code = int(read_bytes(os.path.join(test_dir, 'expected_exit_code')).strip())
            run_dir = os.path.join(work_dir, name)
            os.makedirs(run_dir)

            if mode == 'driver':
                build = manifest['build']
                exe = os.path.join(run_dir, 'test')
                lib_dir = os.path.dirname(lib_path)
                lib_name = os.path.basename(lib_path)[len('lib'):-len('.so')]
                cmd = [build.get('cc', 'cc'), *build.get('cflags', [])]
                cmd += ['-I' + os.path.join(root, d) for d in build.get('include_dirs', [])]
                if build.get('force_include'):
                    cmd += ['-include', os.path.join(tests_dir, build['force_include'])]
                cmd += [os.path.join(test_dir, 'driver.c'),
                    '-L' + lib_dir, '-l' + lib_name, '-Wl,-rpath,' + lib_dir,
                    *build.get('ldflags', []), '-o', exe]
                p = subprocess.run(cmd, stdout = subprocess.PIPE, stderr = subprocess.STDOUT)
                if p.returncode != 0:
                    status, detail = 'build_failed', f'$ {shlex.join(cmd)}\n{tail(p.stdout, 4000)}'
                else:
                    env = dict(os.environ)
                    env['LD_LIBRARY_PATH'] = lib_dir + os.pathsep + env.get('LD_LIBRARY_PATH', '')
                    code, out, err = run_process([exe], run_dir, stdin_path, timeout, env = env)
                    status, detail = judge(code, out, err, expected_out, expected_code,
                        args.compare, timeout, args.float_rel_tol)
            else:
                with open(os.path.join(test_dir, 'args.json')) as f:
                    test_args = json.load(f)
                cmd = run_cmd + (' ' + shlex.join(test_args) if test_args else '')
                cwd = os.path.join(root, manifest.get('project_dir', '.'))
                code, out, err = run_process(cmd, cwd, stdin_path, timeout, shell = True)
                status, detail = judge(code, out, err, expected_out, expected_code,
                    args.compare, timeout, args.float_rel_tol)

            if status == 'pass':
                print(f'PASS {name}')
            else:
                print(f'FAIL {name} ({status})')
                if test.get('description'):
                    print(f'  test: {test["description"]}')
                for line in detail.splitlines():
                    print(f'  {line}')
            sys.stdout.flush()
            results.append({'name': name, 'status': status, 'detail': detail})
    finally:
        if args.keep_work_dir:
            print(f'kept work dir {work_dir}')
        else:
            shutil.rmtree(work_dir, ignore_errors = True)

    if any(r['status'] == 'build_failed' for r in results) and mode == 'driver':
        print('\nnote: test drivers include the project\'s C headers; make sure the C code is '
            f'checked out under {root}')
    passed = sum(1 for r in results if r['status'] == 'pass')
    print(f'\n{passed} passed, {len(results) - passed} failed, {len(results)} total')
    if args.json_report:
        with open(args.json_report, 'w') as f:
            json.dump({'mode': mode, 'results': results}, f, indent = 2)
    return 0 if passed == len(results) else 1


if __name__ == '__main__':
    sys.exit(main())
