from contextlib import contextmanager
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from crisp import gen_tests
from crisp.config import Config
from crisp.error import CrispError
from crisp.gen_tests import (
    FragmentSpec, GenTestsSettings, Observation, TestCase, build_prompt,
    build_tests_tree, check_records, classify, dedupe_names, extract_fragments,
    find_function_lines, find_included_headers, mask_c_source,
    parse_fragment_spec, parse_tests, plan_driver_build,
)
from crisp.mvir import MVIR, FileNode, TreeNode


UTIL_C = '''#include <stdio.h>
#include "util.h"

static const char *braces = "}{ not code";

/* Adds two numbers.
 * Handles overflow by wrapping. */
int add(int a, int b)
{
    // a comment with a } brace
    return a + b;
}

int mul(int a, int b);

int call_add(void) { return add(1, '}'); }

static int
clamp(int x,
      int lo, int hi) {
    if (x < lo) { return lo; }
    if (x > hi) { return hi; }
    return x;
}
'''

UTIL_H = '''#ifndef UTIL_H
#define UTIL_H
int add(int a, int b);
#endif
'''

MAIN_C = '''#include "util.h"
int main(int argc, char **argv) {
    return add(argc, 1);
}
'''


class MaskTest(unittest.TestCase):
    def test_preserves_length_and_newlines(self):
        masked = mask_c_source(UTIL_C)
        self.assertEqual(len(masked), len(UTIL_C))
        self.assertEqual(masked.count('\n'), UTIL_C.count('\n'))

    def test_blanks_comments_strings_and_directives(self):
        masked = mask_c_source(UTIL_C)
        self.assertNotIn('#include', masked)
        self.assertNotIn('not code', masked)
        self.assertNotIn('a comment', masked)
        self.assertNotIn('Adds two', masked)
        self.assertIn('int add(int a, int b)', masked)


class FindFunctionTest(unittest.TestCase):
    def lines(self, start, end):
        return ''.join(UTIL_C.splitlines(keepends = True)[start - 1 : end])

    def test_includes_leading_comment(self):
        start, end = find_function_lines(UTIL_C, 'add')
        self.assertTrue(self.lines(start, start).startswith('/* Adds two numbers.'))
        self.assertEqual(self.lines(end, end), '}\n')
        self.assertIn('return a + b;', self.lines(start, end))

    def test_skips_prototypes(self):
        self.assertIsNone(find_function_lines(UTIL_C, 'mul'))

    def test_one_line_definition(self):
        start, end = find_function_lines(UTIL_C, 'call_add')
        self.assertEqual(start, end)

    def test_multiline_signature_with_static(self):
        start, end = find_function_lines(UTIL_C, 'clamp')
        text = self.lines(start, end)
        self.assertTrue(text.startswith('static int\nclamp('))
        self.assertTrue(text.endswith('return x;\n}\n'))

    def test_main(self):
        self.assertEqual(find_function_lines(MAIN_C, 'main'), (2, 4))
        self.assertIsNone(find_function_lines(UTIL_C, 'main'))


class FragmentSpecTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_fragment_spec('src/a.c'), FragmentSpec('src/a.c'))
        self.assertEqual(parse_fragment_spec('src/a.c:3-7'), FragmentSpec('src/a.c', lines = (3, 7)))
        self.assertEqual(parse_fragment_spec('src/a.c:5'), FragmentSpec('src/a.c', lines = (5, 5)))
        self.assertEqual(parse_fragment_spec('src/a.c:foo'), FragmentSpec('src/a.c', function = 'foo'))
        self.assertEqual(parse_fragment_spec('foo'), FragmentSpec(None, function = 'foo'))

    def test_bad_ranges(self):
        with self.assertRaises(CrispError):
            parse_fragment_spec('a.c:7-3')
        with self.assertRaises(CrispError):
            parse_fragment_spec('a.c:x y')


class MvirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.mvir = MVIR(self._tmp.name, '.')
        self.c_code = TreeNode.new(self.mvir, files = {
            'proj/src/util.c': FileNode.new(self.mvir, UTIL_C).node_id(),
            'proj/include/util.h': FileNode.new(self.mvir, UTIL_H).node_id(),
            'proj/src/main.c': FileNode.new(self.mvir, MAIN_C).node_id(),
        })

    def tearDown(self):
        self._tmp.cleanup()


class ExtractFragmentsTest(MvirTestCase):
    def extract(self, *specs):
        return extract_fragments(self.mvir, self.c_code,
            [parse_fragment_spec(s) for s in specs], cwd_rel = 'proj')

    def test_function_by_name_anywhere(self):
        [frag] = self.extract('clamp')
        self.assertEqual(frag.path, 'proj/src/util.c')
        self.assertEqual(frag.function, 'clamp')
        self.assertIn('return x;', frag.text)

    def test_paths_are_relative_to_cwd(self):
        [frag] = self.extract('src/util.c:1-2')
        self.assertEqual((frag.path, frag.start_line, frag.end_line), ('proj/src/util.c', 1, 2))
        self.assertEqual(frag.text, '#include <stdio.h>\n#include "util.h"\n')

    def test_unique_suffix_match(self):
        [frag] = self.extract('util.h')
        self.assertEqual(frag.path, 'proj/include/util.h')
        self.assertEqual(frag.text, UTIL_H)

    def test_duplicates_removed(self):
        self.assertEqual(len(self.extract('add', 'src/util.c:add')), 1)

    def test_errors(self):
        with self.assertRaises(CrispError):
            self.extract('no_such_function')
        with self.assertRaises(CrispError):
            self.extract('src/missing.c')
        with self.assertRaises(CrispError):
            self.extract('src/util.c:mul')

    def test_included_headers(self):
        self.assertEqual(find_included_headers(self.mvir, self.c_code, 'proj/src/util.c'),
            ['proj/include/util.h'])

    def test_driver_build_excludes_main(self):
        settings = GenTestsSettings(mode = 'driver')
        build = plan_driver_build(self.mvir, self.c_code, settings, None)
        self.assertEqual(build.sources, ('proj/src/util.c',))
        self.assertEqual(build.excluded, ('proj/src/main.c',))
        self.assertEqual(build.include_dirs, ('.', 'proj/include', 'proj/src'))

    def test_prompt_contains_fragments_and_context(self):
        settings = GenTestsSettings(mode = 'driver', num_tests = 4,
            extra_instructions = 'Focus on overflow.')
        frags = self.extract('add')
        build = plan_driver_build(self.mvir, self.c_code, settings, None)
        prompt = build_prompt(self.mvir, self.c_code, frags, settings, build,
            project = 'demo')
        self.assertIn('C library `demo`', prompt)
        self.assertIn('return a + b;', prompt)
        self.assertIn('obs_ll', prompt)
        # Headers are included by their path in the tree.
        self.assertIn('#include "proj/include/util.h"', prompt)
        # The build command shown links everything except the file with `main`.
        self.assertIn('-I. -Iproj/include -Iproj/src test.c proj/src/util.c -o test -lm', prompt)
        # Header included by the fragment's file is added as context.
        self.assertIn('#ifndef UTIL_H', prompt)
        self.assertIn('Write 4 tests.', prompt)
        self.assertIn('Focus on overflow.', prompt)
        self.assertIn('`proj/src/main.c`', prompt)
        self.assertIn('<driver>', prompt)
        self.assertNotIn('<<', prompt)

    def test_cli_prompt(self):
        settings = GenTestsSettings(mode = 'cli', run_cmd = './build/prog')
        prompt = build_prompt(self.mvir, self.c_code, self.extract('add'), settings, None)
        self.assertIn('./build/prog <args...>', prompt)
        self.assertNotIn('obs_ll', prompt)
        self.assertNotIn('<<', prompt)
        self.assertIn('<args>', prompt)
        self.assertNotIn('<driver>', prompt)


DRIVER_RESPONSE = '''Here are the tests.

<test name="Add Small Numbers">
<description>
Adds 1 and 2.
</description>
<driver>
```c
#include <stdio.h>
#include "util.h"
int main(void) { printf("add(1, 2) = %d\\n", add(1, 2)); return 0; }
```
</driver>
</test>

<test name="add_small_numbers">
<description>Duplicate name.</description>
<driver>
int main(void) { return 0; }
</driver>
<stdin>
line one
</stdin>
</test>

<test name="empty">
<description>No driver.</description>
</test>
'''

CLI_RESPONSE = '''<test name="help">
<description>Prints usage.</description>
<args>["--help"]</args>
</test>
<test name="bad_args">
<args>--help</args>
</test>
<test name="no_args">
<args>[]</args>
<stdin>
hello
</stdin>
</test>
'''


class ParseTestsTest(unittest.TestCase):
    def test_driver(self):
        tests, warnings = parse_tests(DRIVER_RESPONSE, 'driver')
        self.assertEqual([t.name for t in tests], ['add_small_numbers', 'add_small_numbers'])
        self.assertEqual(len(warnings), 1)
        # Code fences inside <driver> are removed.
        self.assertTrue(tests[0].driver.startswith('#include <stdio.h>\n'))
        self.assertTrue(tests[0].driver.endswith('return 0; }\n'))
        self.assertEqual(tests[0].description, 'Adds 1 and 2.')
        self.assertEqual(tests[0].stdin, '')
        self.assertEqual(tests[1].stdin, 'line one\n')

        deduped = dedupe_names(tests)
        self.assertEqual([t.name for t in deduped], ['add_small_numbers', 'add_small_numbers_2'])

    def test_cli(self):
        tests, warnings = parse_tests(CLI_RESPONSE, 'cli')
        self.assertEqual([(t.name, t.args) for t in tests],
            [('help', ('--help',)), ('no_args', ())])
        self.assertEqual(tests[1].stdin, 'hello\n')
        self.assertEqual(len(warnings), 1)


def obs(out1 = b'ok\n', out2 = None, code1 = 0, code2 = None, build_failed = False):
    return Observation(
        build_failed = build_failed,
        build_log = b'error: oops',
        stdout = (out1, out1 if out2 is None else out2),
        stderr = (b'', b''),
        exit_code = (code1, code1 if code2 is None else code2),
    )

class ClassifyTest(unittest.TestCase):
    def status(self, o):
        return classify(o, timeout = 10, max_output_bytes = 100).status

    def test_statuses(self):
        self.assertEqual(self.status(obs()), 'ok')
        # Nonzero exit codes are legitimate observed behavior.
        self.assertEqual(self.status(obs(code1 = 3)), 'ok')
        self.assertEqual(self.status(obs(build_failed = True)), 'build_failed')
        self.assertEqual(self.status(obs(code1 = 124)), 'timeout')
        self.assertEqual(self.status(obs(code1 = 139)), 'crashed')
        self.assertEqual(self.status(obs(code1 = 127)), 'run_error')
        self.assertEqual(self.status(obs(out1 = b'x' * 101)), 'output_too_large')
        self.assertEqual(self.status(obs(out2 = b'different\n')), 'nondeterministic')
        self.assertEqual(self.status(obs(code2 = 1)), 'nondeterministic')
        self.assertEqual(self.status(obs(out1 = None)), 'error')

    def driver_status(self, o):
        return classify(o, timeout = 10, max_output_bytes = 100, mode = 'driver').status

    def test_driver_rules(self):
        self.assertEqual(self.driver_status(obs(out1 = b'a/b = 1\nlib output\n')), 'ok')
        # Drivers record values, so they must exit 0.
        self.assertEqual(self.driver_status(obs(out1 = b'a = 1\n', code1 = 3)), 'nonzero_exit')
        self.assertEqual(self.driver_status(obs(out1 = b'no records\n')), 'bad_records')
        self.assertEqual(self.driver_status(obs(out1 = b'a = 1\na = 2\n')), 'bad_records')

    def test_check_records(self):
        self.assertIsNone(check_records(b'x/0 = 1\nx/1 = "s"\n'))
        self.assertIn('more than once', check_records(b'x = 1\nx = 1\n'))
        self.assertIn('no values', check_records(b''))


class StaticCheckTest(unittest.TestCase):
    def status(self, body, focus = ('add',)):
        test = TestCase('t', '', driver = body)
        v = gen_tests.static_check(test, list(focus))
        return None if v is None else v.status

    def test_rules(self):
        self.assertIsNone(self.status('int main(void) { obs_ll("x", add(1, 2)); return 0; }'))
        self.assertEqual(self.status('#include "src/lib.c"\nint main(void) { add(1, 2); }'),
            'includes_c_file')
        self.assertEqual(self.status('int main(void) { freopen("x", "w", stdout); add(1, 2); }'),
            'redirects_output')
        self.assertEqual(self.status('int main(void) { dup2(1, 2); add(1, 2); }'), 'redirects_output')
        self.assertEqual(self.status(
            'void obs_ll(const char *l, long long v) { }\nint main(void) { add(1, 2); }'), 'defines_obs')
        self.assertEqual(self.status('int main(void) { obs_ll("x", 1); return 0; }'), 'misses_focus')

    def test_no_false_positives_in_comments_and_strings(self):
        self.assertIsNone(self.status(
            '/* do not use dup2 or freopen */\nint main(void) { obs_str("add(", "dup2"); add(1, 2); }'))
        # Mentioning the function only in a comment or label doesn't count as calling it.
        self.assertEqual(self.status('// add(1, 2)\nint main(void) { obs_ll("add(", 1); }'),
            'misses_focus')

    def test_focus_skips_static_functions(self):
        frags = [gen_tests.CFragment('a.c', 1, 1, 'static int helper(void) { return 1; }', 'helper'),
            gen_tests.CFragment('a.c', 2, 2, 'int api(void) { return helper(); }', 'api')]
        self.assertEqual(gen_tests.focus_functions(frags), ['api'])


class RecordBoundsTest(unittest.TestCase):
    def test_min_and_max(self):
        out = b''.join(b'r/%d = %d\n' % (i, i) for i in range(5))
        self.assertIsNone(check_records(out, min_records = 5, max_records = 5))
        self.assertIn('at least 6', check_records(out, min_records = 6))
        self.assertIn('at most 4', check_records(out, max_records = 4))


class ParseDropsTest(unittest.TestCase):
    def test_parse(self):
        text = ('<drop name="Abort Path">Targets abort().</drop>\n'
            '<test name="x"><driver>int main(void) { return 0; }</driver></test>')
        self.assertEqual(gen_tests.parse_drops(text), {'abort_path': 'Targets abort().'})
        self.assertEqual(gen_tests.parse_drops('no drops here'), {})


class FloatCompareTest(unittest.TestCase):
    def test_tolerance(self):
        from crisp.gen_tests_runner import outputs_match
        tol = 1e-6
        self.assertTrue(outputs_match(b'd = 0.10000000000000001\n', b'd = 0.10000000000000003\n', tol))
        self.assertTrue(outputs_match(b'd = 1e+300\n', b'd = 1.0000000000001e+300\n', tol))
        self.assertTrue(outputs_match(b'd = nan\n', b'd = -nan\n', tol))
        self.assertFalse(outputs_match(b'd = 0.5\n', b'd = 0.6\n', tol))
        self.assertFalse(outputs_match(b'd = inf\n', b'd = -inf\n', tol))
        # Integers and strings must match exactly; labels must agree.
        self.assertFalse(outputs_match(b'n = 1\n', b'n = 2\n', 1.0))
        self.assertFalse(outputs_match(b'a = 0.5\n', b'b = 0.5\n', tol))
        self.assertFalse(outputs_match(b'd = 0.5\n', b'd = 0.5\nextra\n', tol))


class EmptyReplyRetryTest(unittest.TestCase):
    def test_retries_then_continues(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mvir = MVIR(tmp.name, '.')
        c_code = TreeNode.new(mvir, files = {'lib.c': FileNode.new(mvir, 'int add(int a, int b) { return a + b; }\n').node_id()})
        replies = iter(['', 'Sorry, no tests.', DRIVER_RESPONSE])
        calls = []
        def fake_ask(model, messages, stream = True):
            calls.append(list(messages))
            return {}, {}, next(replies)
        cfg = mock.Mock()
        cfg.relative_path = lambda p: os.path.relpath(p, '.') if os.path.isabs(p) else p
        cfg.project_name = 'p'
        cfg.transpile.output_dir = 'rust'
        observed = {}
        def fake_observe(cfg, mvir, c, tests, *a):
            observed['names'] = [t.name for t in tests]
            return {t.name: obs(out1 = b'x = 1\n') for t in tests}
        settings = GenTestsSettings(mode = 'driver', repair_rounds = 0, empty_retries = 2)
        with mock.patch.object(gen_tests, '_ask', fake_ask), \
                mock.patch.object(gen_tests, 'observe_tests', fake_observe):
            gen_tests.generate_tests(cfg, mvir, c_code, [parse_fragment_spec('add')],
                settings, 'crisp_tests', model = 'fake')
        # Two empty replies are retried with the same conversation.
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], calls[2])
        # The second test in DRIVER_RESPONSE never calls `add`, so the static
        # check rejects it before it is run.
        self.assertEqual(observed['names'], ['add_small_numbers'])


class BuildTestsTreeTest(MvirTestCase):
    def test_layout(self):
        t_driver = TestCase('a', 'desc a', driver = 'int main(void) { return 2; }\n', stdin = 'in\n')
        t_cli = TestCase('b', 'desc b', args = ('-v',))
        tree = build_tests_tree(self.mvir, 'proj/crisp_tests',
            [(t_driver, obs(out1 = b'out a\n', code1 = 2)), (t_cli, obs(out1 = b'out b\n'))],
            [], {'mode': 'driver'})
        body = lambda p: self.mvir.node(tree.files[p]).body_str()
        self.assertEqual(sorted(tree.files), [
            'proj/crisp_tests/a/driver.c',
            'proj/crisp_tests/a/expected_exit_code',
            'proj/crisp_tests/a/expected_stdout',
            'proj/crisp_tests/a/stdin',
            'proj/crisp_tests/b/args.json',
            'proj/crisp_tests/b/expected_exit_code',
            'proj/crisp_tests/b/expected_stdout',
            'proj/crisp_tests/crisp_obs.h',
            'proj/crisp_tests/manifest.json',
            'proj/crisp_tests/run_tests.py',
        ])
        self.assertEqual(body('proj/crisp_tests/a/expected_stdout'), 'out a\n')
        self.assertEqual(body('proj/crisp_tests/a/expected_exit_code'), '2\n')
        self.assertEqual(body('proj/crisp_tests/b/args.json'), '["-v"]\n')



class LocalSandbox:
    """
    Minimal stand-in for the real sandboxes that runs commands directly on
    the host in a temporary directory.  Only used for trusted test inputs.
    """
    def __init__(self, mvir, path):
        from crisp.work_dir import WorkDir
        self.work_dir = WorkDir(mvir, path)
        self.path = path

    def checkout(self, n_tree, rel_path = None):
        self.work_dir.checkout(n_tree, rel_path = rel_path)

    def commit_dir(self, rel_path, ignore_spec = None, path_filter = None):
        return self.work_dir.commit_dir(rel_path, ignore_spec = ignore_spec,
            path_filter = path_filter)

    def run(self, cmd, shell = False, stream = False, cwd = '.', env = {}):
        if shell:
            cmd = ['sh', '-c', cmd]
        p = subprocess.run(cmd, cwd = os.path.join(self.path, cwd),
            stdout = subprocess.PIPE, stderr = subprocess.STDOUT)
        return p.returncode, p.stdout

@contextmanager
def local_sandbox(cfg, mvir, require_consistent_path = False):
    with tempfile.TemporaryDirectory() as d:
        yield LocalSandbox(mvir, d)


LIB_C = """#include <stdio.h>
#include "lib.h"

int add(int a, int b) { return a + b; }

int greet(const char *name) {
    if (name == 0) return -1;
    return printf("hello, %s\\n", name);
}
"""

LIB_H = """int add(int a, int b);
int greet(const char *name);
"""

PROG_C = """#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
    char buf[64];
    if (argc > 1 && strcmp(argv[1], "--fail") == 0) return 3;
    while (fgets(buf, sizeof buf, stdin)) fputs(buf, stdout);
    printf("argc=%d\\n", argc);
    return 0;
}
"""

def driver_test(name, body):
    return (f'<test name="{name}">\n<description>{name}</description>\n<driver>\n'
        f'```c\n#include <stdlib.h>\n#include "lib.h"\n'
        f'int main(void) {{ {body} }}\n```\n</driver>\n</test>\n')

DRIVER_ROUND_1 = (
    driver_test('adds', 'obs_ll("add/2_3", add(2, 3)); obs_ll("add/neg", add(-4, 1)); return 0;')
    + driver_test('greets', 'obs_ll("greet/bob", greet("bob")); obs_ll("greet/null", greet(0)); return 0;')
    + driver_test('broken', 'add(0, 0); return no_such_function();')
    + driver_test('crashes', 'add(0, 0); abort();')
    + driver_test('random', 'int x = add(0, 0); obs_ll("addr", (long long)&x); return 0;')
    + driver_test('fails', 'obs_ll("x", add(0, 1)); return 1;')
    + driver_test('dup', 'obs_ll("x", add(0, 1)); obs_ll("x", 2); return 0;')
    + driver_test('no_focus', 'obs_ll("x", 1); return 0;')
    # Replaces libc's `strlen`; linked against Rust, this would shadow libc.
    + driver_test('interposer', 'obs_ll("x", add(1, 1)); return 0; } size_t strlen(const char *s) { return 0;')
)
DRIVER_ROUND_2 = driver_test('broken',
    'obs_str("s", "a\\"b\\n"); obs_dbl("d", 0.5); obs_dbl("i", 2); obs_ll("sum", add(1, 2)); return 0;') \
    + '<drop name="crashes">It tests the abort path, which cannot be tested.</drop>\n'


# Hand-written stand-in for a translation of LIB_C, exporting the same FFI
# entry points.  `greet` prints with Rust's `print!` rather than libc, as a
# safety-refactored translation might.
RUST_LIB_RS = """use std::ffi::{c_char, c_int, CStr};

#[no_mangle]
pub extern "C" fn add(a: c_int, b: c_int) -> c_int {
    ADD_BODY
}

#[no_mangle]
pub unsafe extern "C" fn greet(name: *const c_char) -> c_int {
    if name.is_null() {
        return -1;
    }
    let line = format!("hello, {}\\n", CStr::from_ptr(name).to_string_lossy());
    print!("{}", line);
    line.len() as c_int
}
"""


@unittest.skipUnless(shutil.which('cc') and shutil.which('timeout'),
    'requires a C compiler and timeout')
class GenerateTestsIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = self._tmp.name
        with open(os.path.join(self.proj, 'crisp.toml'), 'w') as f:
            f.write('project_name = "p"\nsrc_globs = "rust/src/*.rs"\n'
                '[transpile]\noutput_dir = "rust"\n'
                '[[transpile.artifacts]]\nname = "p"\nbuild_cmds = "cc -o prog prog.c"\n')
        self._old_cwd = os.getcwd()
        os.chdir(self.proj)
        with open('crisp.toml') as f:
            self.cfg = Config.from_toml_file(f)
        self.mvir = MVIR(self.cfg.mvir_storage_dir, '.')

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def run_gen(self, files, specs, settings, responses):
        c_code = TreeNode.new(self.mvir, files = {
            k: FileNode.new(self.mvir, v).node_id() for k, v in files.items()})
        replies = iter(responses)
        def fake_ask(model, messages, stream = True):
            self.messages = messages
            return {'messages': messages}, {'reply': True}, next(replies)
        with mock.patch.object(gen_tests, 'run_sandbox', local_sandbox), \
                mock.patch.object(gen_tests, '_ask', fake_ask):
            result = gen_tests.generate_tests(self.cfg, self.mvir, c_code,
                [parse_fragment_spec(s) for s in specs], settings,
                os.path.join(self.proj, 'crisp_tests'), model = 'fake',
                art = self.cfg.transpile.artifact(None))
        body = lambda p: self.mvir.node(result.tests.files[p]).body_str()
        return result, body

    def test_driver_mode(self):
        settings = GenTestsSettings(mode = 'driver', num_tests = 5, timeout = 5,
            repair_rounds = 1, rerun_delay = 0)
        result, body = self.run_gen({'lib.c': LIB_C, 'lib.h': LIB_H},
            ['add', 'greet'], settings, [DRIVER_ROUND_1, DRIVER_ROUND_2])

        self.assertEqual(body('crisp_tests/adds/expected_stdout'), 'add/2_3 = 5\nadd/neg = -3\n')
        self.assertEqual(body('crisp_tests/adds/expected_exit_code'), '0\n')
        # The library's own output is kept alongside the records.
        self.assertEqual(body('crisp_tests/greets/expected_stdout'),
            'hello, bob\ngreet/bob = 11\ngreet/null = -1\n')
        # `broken` failed to build in round 1 and was repaired in round 2.
        # Doubles are always printed with a `.`, so they can be compared with tolerance.
        self.assertEqual(body('crisp_tests/broken/expected_stdout'),
            's = "a\\"b\\x0a"\nd = 0.5\ni = 2.0\nsum = 3\n')
        self.assertIn('obs_str', body('crisp_tests/crisp_obs.h'))

        repair = self.messages[2]['content']
        self.assertIn('no_such_function', repair)
        self.assertIn('Your previous test:', repair)
        for status in ('build_failed', 'crashed', 'nondeterministic', 'nonzero_exit', 'bad_records',
                'misses_focus', 'interposes'):
            self.assertIn(status, repair)

        self.assertIn('`strlen`', repair)
        self.assertEqual((result.num_accepted, result.num_rejected), (3, 6))
        manifest = __import__('json').loads(body('crisp_tests/manifest.json'))
        self.assertEqual(sorted(r['name'] for r in manifest['rejected']),
            ['crashes', 'dup', 'fails', 'interposer', 'no_focus', 'random'])
        # The LLM gave up on `crashes` in the repair round.
        [crashes] = [r for r in manifest['rejected'] if r['name'] == 'crashes']
        self.assertEqual(crashes['status'], 'dropped')
        self.assertIn('abort path', crashes['reason'])
        self.assertEqual(self.mvir.tag('generated_tests'), result.tests.node_id())
        self.assertEqual(self.mvir.tag('gen_tests_record'), result.record.node_id())

    @unittest.skipUnless(shutil.which('cargo'), 'requires cargo')
    def test_driver_tests_run_against_rust_cdylib(self):
        from crisp import analysis, gen_tests_runner, run_gen_tests
        from crisp.work_dir import WorkDir

        settings = GenTestsSettings(mode = 'driver', timeout = 5, repair_rounds = 0, rerun_delay = 0)
        response = (
            driver_test('adds', 'obs_ll("add/2_3", add(2, 3)); obs_ll("add/neg", add(-4, 1)); return 0;')
            + driver_test('greets', 'obs_ll("greet/bob", greet("bob")); obs_ll("greet/null", greet(0)); return 0;'))
        c_files = {'lib.c': LIB_C, 'lib.h': LIB_H}
        result, body = self.run_gen(c_files, ['add', 'greet'], settings, [response])
        self.assertEqual(result.num_accepted, 2)

        manifest = __import__('json').loads(body('crisp_tests/manifest.json'))
        self.assertEqual((manifest['rust_dir'], manifest['artifact'], manifest['root_from_tests']),
            ('rust', 'p', '..'))

        def rust_tree(add_body):
            files = {
                'rust/Cargo.toml': '[package]\nname = "p"\nversion = "0.1.0"\nedition = "2021"\n'
                    '[lib]\nname = "p"\npath = "src/lib.rs"\ncrate-type = ["cdylib"]\n',
                'rust/src/lib.rs': RUST_LIB_RS.replace('ADD_BODY', add_body),
            }
            return TreeNode.new(self.mvir, files = {
                k: FileNode.new(self.mvir, v).node_id() for k, v in files.items()})

        # Run the bundled runner directly, as a user would after `crisp checkout`.
        # The drivers include the project's C headers, so the C code is on disk too.
        c_code = TreeNode.new(self.mvir, files = {
            k: FileNode.new(self.mvir, v).node_id() for k, v in c_files.items()})
        for tree in (c_code, result.tests, rust_tree('a.wrapping_add(b)')):
            WorkDir(self.mvir, self.proj).checkout(tree)
        report = os.path.join(self.proj, 'report.json')
        code = gen_tests_runner.main(['--tests-dir', os.path.join(self.proj, 'crisp_tests'),
            '--json-report', report])
        with open(report) as f:
            results = __import__('json').load(f)['results']
        self.assertEqual(code, 0, results)
        self.assertEqual([r['status'] for r in results], ['pass', 'pass'])

        # Through the sandbox wrapper: a correct translation passes, and an
        # off-by-one in the Rust `add` is caught through the FFI entry point.
        with mock.patch.object(analysis, 'run_sandbox', local_sandbox):
            good = run_gen_tests.run_gen_tests(self.cfg, self.mvir,
                rust_tree('a.wrapping_add(b)'), c_code, result.tests)
            bad = run_gen_tests.run_gen_tests(self.cfg, self.mvir,
                rust_tree('if a < 0 { a + b + 1 } else { a + b }'), c_code, result.tests)
        self.assertTrue(good.passed, good.body_str())
        self.assertFalse(bad.passed)
        log = bad.body_str()
        self.assertIn('PASS greets', log)
        self.assertIn('FAIL adds (wrong_output)', log)
        self.assertIn('-add/neg = -3', log)
        self.assertIn('+add/neg = -2', log)
        self.assertEqual(self.mvir.tag('gen_tests_results'), bad.node_id())

    def test_cli_mode(self):
        settings = GenTestsSettings(mode = 'cli', run_cmd = './prog', repair_rounds = 0,
            rerun_delay = 0)
        response = (
            '<test name="echo">\n<args>["a", "b c"]</args>\n<stdin>\nhi\n</stdin>\n</test>\n'
            '<test name="fails">\n<args>["--fail"]</args>\n</test>\n')
        result, body = self.run_gen({'prog.c': PROG_C}, ['prog.c:main'], settings, [response])

        self.assertEqual(body('crisp_tests/echo/expected_stdout'), 'hi\nargc=3\n')
        self.assertEqual(body('crisp_tests/echo/stdin'), 'hi\n')
        self.assertEqual(body('crisp_tests/echo/args.json'), '["a", "b c"]\n')
        self.assertEqual(body('crisp_tests/fails/expected_exit_code'), '3\n')
        self.assertEqual((result.num_accepted, result.num_rejected), (2, 0))


if __name__ == '__main__':
    unittest.main()
