import contextlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crisp import analysis
from crisp.config import Config, TranspileConfig
from crisp.mvir import MVIR, FileNode, TreeNode
from crisp.work_dir import WorkDir


class ObservationAnalysisTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.mvir = MVIR(directory.name, '.')
        self.c_code = self.tree({'api.h': 'int api(void);'})
        self.tests = self.tree({'crisp_tests/tests/basic.c': '// RUN: test_basic\n'})
        self.rust = self.tree({'rust/src/lib.rs': 'fn api() {}'})
        self.binaries = self.tree({'crisp_tests/build/bin/test_basic': b'original executable'})
        self.checkouts = []
        self.commands = []
        self.builds = 0
        self.runs = 0
        self.exit_code = 0
        self.have_results = True
        self.build_exit_code = None
        self.exit_codes_by_rust = {}
        self.output_files_by_rust = {}
        self.cfg = Config(
            config_path='crisp.toml', project_name='observed', src_globs=['rust/**/*.rs'],
            transpile=TranspileConfig(config_path='crisp.toml', output_dir='rust', artifacts=[]))

        @contextlib.contextmanager
        def sandbox(cfg, mvir):
            self.checkouts.append([])
            yield SimpleNamespace(checkout=self.checkout, run=self.sandbox_run,
                                  commit_dir=self.commit_dir)

        replacement = patch('crisp.analysis.run_sandbox', sandbox)
        self.addCleanup(replacement.stop)
        replacement.start()

    def tree(self, files):
        return TreeNode.new(self.mvir, files={
            name: FileNode.new(self.mvir, body).node_id() for name, body in files.items()})

    def checkout(self, tree):
        self.checkouts[-1].append(tree.node_id())

    def sandbox_run(self, cmd, stream=False):
        self.commands.append(cmd)
        if cmd[:2] == ['mkdir', '-p']:
            return 0, b''
        if cmd == ['test', '-f', 'crisp_tests/output/results.json']:
            return (0 if self.have_results else 1), b''
        if cmd[1] == 'build':
            self.builds += 1
            if self.build_exit_code is not None:
                return self.build_exit_code, b'compile failure logs'
        else:
            self.runs += 1
            rust = self.checkouts[-1][0]
            if rust in self.exit_codes_by_rust:
                return self.exit_codes_by_rust[rust], b'run failure logs'
        return self.exit_code, b'runner logs'

    def commit_dir(self, path):
        if path == 'crisp_tests/build':
            return self.binaries
        self.assertEqual(path, 'crisp_tests/output')
        rust = self.checkouts[-1][0]
        if rust in self.output_files_by_rust:
            return self.tree({'crisp_tests/output/' + name: body
                              for name, body in self.output_files_by_rust[rust].items()})
        files = {'crisp_tests/output/tests/basic.c.stdout': b'\x00\xffobservation',
                 'crisp_tests/output/tests/basic.c.stderr': b'',
                 'crisp_tests/output/tests/basic.c.json': '{"returncode": 0, "status": "exited"}'}
        if self.have_results:
            files['crisp_tests/output/results.json'] = '{"tests": []}'
        return self.tree(files)

    def compile(self, c_code=None, tests=None, rust=None):
        return analysis.compile_tests(
            self.cfg, self.mvir, c_code or self.c_code, rust or self.rust,
            tests or self.tests)

    def observe(self, rust=None, tests=None, binaries=None, trial=0):
        return analysis.run_observation_tests(
            self.cfg, self.mvir, rust or self.rust, binaries or self.binaries,
            tests or self.tests, trial=trial)

    def candidate(self, extra=None):
        return TreeNode.new(self.mvir, files={
            **self.rust.files, **self.tests.files,
            **{name: FileNode.new(self.mvir, body).node_id()
               for name, body in (extra or {}).items()}})

    def compare(self, new_code=None, old_code=None, c_code=None):
        return analysis.compare_observation_tests(
            self.cfg, self.mvir, old_code or self.rust,
            new_code or self.candidate(), c_code or self.c_code)

    def test_compile_is_cached_by_all_input_nodes(self):
        original = self.compile()
        self.assertEqual(self.compile().node_id(), original.node_id())
        self.assertEqual(self.builds, 1)
        self.compile(c_code=self.tree({'api.h': 'int api(int);'}))
        self.compile(tests=self.tree({'crisp_tests/tests/basic.c': 'changed test'}))
        self.compile(rust=self.tree({'rust/src/lib.rs': 'changed baseline'}))
        self.assertEqual(self.builds, 4)
        self.assertEqual(self.checkouts[0], [
            self.c_code.node_id(), self.rust.node_id(), self.tests.node_id()])
        binaries = self.mvir.node(original.test_binaries)
        self.assertEqual(set(binaries.files), {'crisp_tests/build/bin/test_basic'})
        self.assertEqual(original.cmd, [
            'crisp_test_runner.py', 'build', '--suite-dir', 'crisp_tests',
            '--c-dir', '.', '--rust-dir', 'rust', '--build-dir', 'crisp_tests/build',
            '--sanitize'])
        self.assertEqual(original.body(), b'runner logs')

    def test_run_is_cached_by_all_input_nodes_and_trial(self):
        original = self.observe()
        self.assertEqual(self.observe().node_id(), original.node_id())
        self.assertEqual(self.runs, 1)
        self.observe(rust=self.tree({'rust/src/lib.rs': 'changed candidate'}))
        self.observe(tests=self.tree({'crisp_tests/tests/basic.c': 'changed test'}))
        self.observe(binaries=self.tree({'crisp_tests/build/bin/test_basic': 'changed executable'}))
        repeated = self.observe(trial=1)
        self.assertEqual(self.runs, 5)
        self.assertNotEqual(original.node_id(), repeated.node_id())
        self.assertEqual(self.checkouts[0], [
            self.rust.node_id(), self.binaries.node_id(), self.tests.node_id()])
        outputs = self.mvir.node(original.outputs)
        self.assertEqual(set(outputs.files), {
            'crisp_tests/output/' + name for name in (
                'results.json', 'tests/basic.c.stdout', 'tests/basic.c.stderr', 'tests/basic.c.json')})
        self.assertEqual(self.mvir.node(outputs.files['crisp_tests/output/tests/basic.c.stdout']).body(),
                         b'\x00\xffobservation')
        self.assertEqual(original.cmd, [
            'crisp_test_runner.py', 'run', '--suite-dir', 'crisp_tests',
            '--rust-dir', 'rust', '--build-dir', 'crisp_tests/build',
            '--output-dir', 'crisp_tests/output'])

    def test_build_failure_retains_logs_without_publishing_binaries(self):
        self.exit_code = 1
        result = self.compile()
        self.assertEqual(result.exit_code, 1)
        self.assertIsNone(result.test_binaries)
        self.assertEqual(result.body(), b'runner logs')

    def test_run_failure_retains_partial_observations(self):
        self.exit_code = 1
        result = self.observe()
        self.assertEqual(result.exit_code, 1)
        self.assertIsNotNone(result.outputs)
        self.have_results = False
        partial = self.observe(trial=1)
        self.assertNotIn('crisp_tests/output/results.json', self.mvir.node(partial.outputs).files)
        self.assertIn('crisp_tests/output/tests/basic.c.stdout', self.mvir.node(partial.outputs).files)

    def test_comparison_extracts_only_current_suite_and_uses_three_sandboxes(self):
        suite_files = {
            'crisp_tests/tests.toml': 'cflags = []', 'crisp_tests/tests/basic.c': 'current test',
            'crisp_tests/test_helpers/common.c': 'helper',
            'crisp_tests/test_helpers/common.h': 'header',
            'crisp_tests/fixtures/nested/input.txt': 'fixture'}
        candidate = self.candidate({**suite_files, 'README': 'excluded',
                                    'crisp_tests.bak/tests/basic.c': 'excluded',
                                    'crisp_tests/build/bin/test_basic': 'excluded',
                                    'crisp_tests/output/results.json': 'excluded',
                                    'rust/tests/internal.rs': 'excluded'})
        old = self.tree({**{'rust/src/lib.rs': 'original Rust'},
                         'tests/obsolete.c': 'obsolete test', 'fixtures/old.txt': 'old fixture'})
        result = self.compare(candidate, old_code=old)
        self.assertTrue(result.passed, result.body_str())
        compiled = self.mvir.node(result.compile_op)
        suite = self.mvir.node(compiled.test_code)
        self.assertEqual(set(suite.files), set(suite_files))
        self.assertEqual({name: self.mvir.node(node_id).body_str()
                          for name, node_id in suite.files.items()}, suite_files)
        reference = self.mvir.node(result.old_run)
        current = self.mvir.node(result.new_run)
        self.assertEqual(reference.outputs, current.outputs)
        self.assertEqual(current.test_binaries, compiled.test_binaries)
        self.assertEqual(reference.test_binaries, compiled.test_binaries)
        self.assertEqual(reference.test_code, suite.node_id())
        self.assertIsNone(current.test_code)
        self.assertEqual((self.builds, self.runs), (1, 2))
        self.assertEqual(self.checkouts, [
            [self.c_code.node_id(), old.node_id(), suite.node_id()],
            [old.node_id(), compiled.test_binaries, suite.node_id()],
            [candidate.node_id(), compiled.test_binaries]])
        self.assertEqual(compiled.cmd, [
            'crisp_test_runner.py', 'build', '--suite-dir', 'crisp_tests',
            '--c-dir', '.', '--rust-dir', 'rust', '--build-dir', 'crisp_tests/build',
            '--sanitize'])
        self.assertTrue(all(self.c_code.node_id() not in checkout
                            for checkout in self.checkouts[1:]))

    def test_comparison_caches_reference_across_rust_edits_and_tracks_suite_inputs(self):
        original = self.compare()
        self.assertEqual(self.compare().node_id(), original.node_id())
        self.assertEqual((self.builds, self.runs), (1, 2))
        changed = self.compare(self.candidate({'rust/src/lib.rs': 'changed Rust'}))
        self.assertEqual(changed.old_run, original.old_run)
        self.assertEqual((self.builds, self.runs), (1, 3))
        for path in ('crisp_tests/tests/basic.c', 'crisp_tests/tests.toml',
                     'crisp_tests/test_helpers/common.h', 'crisp_tests/fixtures/input'):
            changed = self.compare(self.candidate({path: 'changed suite input'}))
            self.assertNotEqual(changed.old_run, original.old_run)
        self.assertEqual((self.builds, self.runs), (5, 11))
        changed_header = self.compare(c_code=self.tree({'api.h': 'changed header'}))
        # Headers affect compilation only. Identical binaries reuse both runs.
        self.assertEqual(changed_header.old_run, original.old_run)
        self.assertEqual((self.builds, self.runs), (6, 11))
        self.compare(old_code=self.tree({'rust/src/lib.rs': 'changed original'}))
        # The fake compiler emits identical binaries, so changing the reference
        # Rust alone can also reuse the previous candidate execution.
        self.assertEqual((self.builds, self.runs), (7, 12))

    def test_comparison_build_failure_skips_runs_and_preserves_feedback(self):
        self.build_exit_code = 2
        result = self.compare()
        self.assertFalse(result.passed)
        self.assertIsNone(result.new_run)
        self.assertEqual((self.builds, self.runs), (1, 0))
        self.assertIsNone(result.old_run)
        self.assertIn('Compiling tests failed', result.body_str())
        self.assertIn('compile failure logs', result.body_str())

    def test_comparison_reference_runtime_failure_skips_candidate(self):
        self.exit_codes_by_rust[self.rust.node_id()] = 1
        result = self.compare()
        self.assertFalse(result.passed)
        self.assertIsNone(result.new_run)
        self.assertIsNotNone(self.mvir.node(result.old_run).outputs)
        self.assertEqual((self.builds, self.runs), (1, 1))
        self.assertIn('run failure logs', result.body_str())

    def test_comparison_rejects_failed_candidate_even_with_matching_partial_outputs(self):
        candidate = self.candidate()
        self.exit_codes_by_rust[candidate.node_id()] = 1
        result = self.compare(candidate)
        self.assertFalse(result.passed)
        self.assertEqual(self.mvir.node(result.old_run).outputs,
                         self.mvir.node(result.new_run).outputs)
        self.assertIn('Tests failed on current implementation', result.body_str())
        self.assertIn('run failure logs', result.body_str())

    def test_comparison_requires_output_snapshot(self):
        self.have_results = False
        result = self.compare()
        self.assertFalse(result.passed)
        self.assertIsNone(result.new_run)
        self.assertEqual(self.runs, 1)
        reference = self.mvir.node(result.old_run)
        self.assertNotEqual(reference.exit_code, 0)
        self.assertNotIn('crisp_tests/output/results.json', self.mvir.node(reference.outputs).files)

    def test_comparison_diffs_streams_exit_metadata_and_arbitrary_bytes(self):
        before = {'results.json': '{"tests": []}', 'tests/basic.c.stdout': b'old observation\n',
                  'tests/basic.c.stderr': b'', 'tests/basic.c.json': '{"returncode": 0}\n'}
        self.output_files_by_rust[self.rust.node_id()] = before
        for index, (path, contents, expected) in enumerate([
                ('tests/basic.c.stdout', b'changed observation\n', 'changed observation'),
                ('tests/basic.c.stderr', b'error\n', '+error'),
                ('tests/basic.c.json', '{"returncode": 7}\n', '+{"returncode": 7}'),
                ('tests/basic.c.stdout', b'\x00\xff', 'Binary files'),
                ('tests/basic.c.stdout', b'\xff', '\\xff'),
                ('tests/new.sh.stdout', b'', 'Only in current-outputs')]):
            with self.subTest(path=path, contents=contents):
                candidate = self.candidate({'rust/src/lib.rs': f'variant {index}'})
                self.output_files_by_rust[candidate.node_id()] = {**before, path: contents}
                result = self.compare(candidate)
                self.assertFalse(result.passed)
                feedback = result.body_str()
                self.assertIn(path, feedback)
                self.assertIn(expected, feedback)
                self.assertNotIn('elapsed_seconds', feedback)
        self.assertEqual(self.builds, 1)


@unittest.skipUnless(shutil.which('cargo') and shutil.which('clang'), 'requires cargo and clang')
class ObservationAnalysisIntegrationTest(unittest.TestCase):
    def test_comparison_builds_reference_once_and_reports_refactoring_regression(self):
        runner = Path(__file__).resolve().parents[1] / 'scripts/crisp_test_runner.py'
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            project_rel = 'foo/bar/baz'
            project = Path(directory) / project_rel
            project.mkdir(parents=True)
            stack.enter_context(contextlib.chdir(project))
            mvir = MVIR(Path(directory) / 'storage', directory)

            def tree(files):
                return TreeNode.new(mvir, files={
                    project_rel + '/' + name: FileNode.new(mvir, body).node_id()
                    for name, body in files.items()})

            config_path = str(project / 'crisp.toml')
            cfg = Config(config_path=config_path, project_name='observed', src_globs=[],
                         base_dir='../../..',
                         transpile=TranspileConfig(
                             config_path=config_path, output_dir='generated/rust', artifacts=[]))
            cargo = '''
[package]
name = "observed"
version = "0.1.0"
edition = "2021"
[lib]
crate-type = ["cdylib"]
'''
            rust = {'generated/rust/Cargo.toml': cargo, 'generated/rust/src/lib.rs': '''
#[no_mangle]
pub extern "C" fn value() -> i32 { 7 }
'''}
            suite = {
                'tests.toml': 'cflags = ["-Iinclude"]\n',
                'test_helpers/common.h': 'int observed_value(void);\n',
                'test_helpers/common.c': '''
#include <api.h>
int observed_value(void) { return value(); }
''',
                'fixtures/input': 'fixture observation\n',
                'tests/value.c': '''
// RUN: test_value && cat "$TEST_ROOT/fixtures/input"
#include <stdio.h>
#include "common.h"
int main(void) { printf("%d\\n", observed_value()); return 0; }
'''}
            suite = {'crisp_tests/' + name: body for name, body in suite.items()}
            c_code = tree({'include/api.h': 'int value(void);\n'})
            initial = tree(rust)
            candidate = tree({**rust, **suite})
            changed = tree({**rust, **suite, 'generated/rust/src/lib.rs': '''
#[no_mangle]
pub extern "C" fn value() -> i32 { 9 }
'''})
            invocations = []

            class LocalSandbox(WorkDir):
                def run(self, cmd, stream=False):
                    if cmd[0] == 'crisp_test_runner.py':
                        invocations.append(cmd[1])
                        cmd = [sys.executable, str(runner), *cmd[1:]]
                    p = subprocess.run(cmd, cwd=self.path, capture_output=True, timeout=30)
                    return p.returncode, p.stdout + p.stderr

            workspaces = []

            @contextlib.contextmanager
            def sandbox(cfg, mvir):
                with tempfile.TemporaryDirectory() as work:
                    workspaces.append(work)
                    yield LocalSandbox(mvir, work)
                    self.assertFalse((Path(work) / 'golden-outputs').exists())

            with patch('crisp.analysis.run_sandbox', sandbox):
                result = analysis.compare_observation_tests(cfg, mvir, initial, candidate, c_code)
                self.assertTrue(result.passed, result.body_str())
                reference = mvir.node(result.old_run)
                current = mvir.node(result.new_run)
                self.assertEqual(reference.outputs, current.outputs)
                compiled = mvir.node(result.compile_op)
                self.assertEqual(compiled.cmd, [
                    'crisp_test_runner.py', 'build',
                    '--suite-dir', project_rel + '/crisp_tests',
                    '--c-dir', project_rel,
                    '--rust-dir', project_rel + '/generated/rust',
                    '--build-dir', project_rel + '/crisp_tests/build', '--sanitize'])
                self.assertEqual(current.test_binaries, compiled.test_binaries)
                self.assertEqual(reference.test_binaries, compiled.test_binaries)
                outputs = mvir.node(reference.outputs)
                stdout_path = project_rel + '/crisp_tests/output/tests/value.c.stdout'
                self.assertEqual(mvir.node(outputs.files[stdout_path]).body(),
                                 b'7\nfixture observation\n')
                self.assertEqual(len(workspaces), 3)
                self.assertEqual(invocations, ['build', 'run', 'run'])
                cached = analysis.compare_observation_tests(cfg, mvir, initial, candidate, c_code)
                self.assertEqual(cached.node_id(), result.node_id())
                regression = analysis.compare_observation_tests(cfg, mvir, initial, changed, c_code)
                self.assertFalse(regression.passed)
                self.assertEqual(regression.old_run, reference.node_id())
                self.assertIn('golden-outputs/' + stdout_path, regression.body_str())
                self.assertIn('current-outputs/' + stdout_path, regression.body_str())
                self.assertIn('-7\n+9\n', regression.body_str())
                self.assertEqual(len(workspaces), 4)
                self.assertEqual(invocations, ['build', 'run', 'run', 'run'])

    def test_build_and_run_mvir_bundles_in_separate_workspaces(self):
        runner = Path(__file__).resolve().parents[1] / 'scripts/crisp_test_runner.py'
        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
            mvir = MVIR(directory, '.')

            def tree(files):
                return TreeNode.new(mvir, files={
                    name: FileNode.new(mvir, body).node_id() for name, body in files.items()})

            config_path = str(Path(directory) / 'crisp.toml')
            cfg = Config(config_path=config_path, project_name='observed', src_globs=[],
                         transpile=TranspileConfig(
                             config_path=config_path, output_dir='rust', artifacts=[]))
            c_code = tree({'api.h': 'int value(void);'})
            tests = tree({'crisp_tests/tests/value.c': '''
// RUN: test_value
#include <stdio.h>
#include <api.h>
int main(void) { printf("%d\\n", value()); return 0; }
'''})
            cargo = '''
[package]
name = "observed"
version = "0.1.0"
edition = "2021"
[lib]
crate-type = ["cdylib"]
'''
            initial = tree({'rust/Cargo.toml': cargo, 'rust/src/lib.rs': '''
#[no_mangle]
pub extern "C" fn value() -> i32 { 7 }
'''})
            candidate = tree({'rust/Cargo.toml': cargo, 'rust/src/lib.rs': '''
#[no_mangle]
pub extern "C" fn value() -> i32 { 9 }
'''})
            invocations = []

            class LocalSandbox(WorkDir):
                def run(self, cmd, stream=False):
                    if cmd[0] == 'crisp_test_runner.py':
                        cmd = [sys.executable, str(runner), *cmd[1:]]
                    p = subprocess.run(cmd, cwd=self.path, capture_output=True, timeout=30)
                    return p.returncode, p.stdout + p.stderr

            @contextlib.contextmanager
            def sandbox(cfg, mvir):
                invocations.append(None)
                with tempfile.TemporaryDirectory() as work:
                    yield LocalSandbox(mvir, work)

            def observation(result):
                self.assertEqual(result.exit_code, 0, result.body_str())
                outputs = mvir.node(result.outputs)
                output_dir = 'crisp_tests/output/'
                report = json.loads(mvir.node(outputs.files[output_dir + 'results.json']).body())
                case = report['tests'][0]
                self.assertNotIn('elapsed_seconds', case)
                metadata = json.loads(mvir.node(outputs.files[output_dir + case['metadata']]).body())
                self.assertEqual(metadata['returncode'], 0)
                return mvir.node(outputs.files[output_dir + case['stdout']]).body()

            with patch('crisp.analysis.run_sandbox', sandbox):
                compiled = analysis.compile_tests(cfg, mvir, c_code, initial, tests)
                self.assertEqual(compiled.exit_code, 0, compiled.body_str())
                binaries = mvir.node(compiled.test_binaries)
                reference = analysis.run_observation_tests(
                    cfg, mvir, initial, binaries, tests)
                self.assertEqual(observation(reference), b'7\n')
                cached = analysis.run_observation_tests(
                    cfg, mvir, initial, binaries, tests)
                self.assertEqual(cached.node_id(), reference.node_id())
                changed = analysis.run_observation_tests(
                    cfg, mvir, candidate, binaries, tests)
                self.assertEqual(observation(changed), b'9\n')
                repeated = analysis.run_observation_tests(
                    cfg, mvir, candidate, binaries, tests, trial=1)
                self.assertEqual(observation(repeated), b'9\n')
                self.assertEqual(repeated.outputs, changed.outputs)
                self.assertEqual(len(invocations), 4)


if __name__ == '__main__':
    unittest.main()
