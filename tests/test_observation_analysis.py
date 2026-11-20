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
from crisp.config import Config, TranspileConfig, TranspileArtifactConfig
from crisp.mvir import MVIR, FileNode, TreeNode
from crisp.work_dir import WorkDir


class ObservationAnalysisTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.mvir = MVIR(directory.name, '.')
        self.c_code = self.tree({'api.h': 'int api(void);'})
        self.tests = self.tree({'tests/basic.c': '// RUN: test_basic\n'})
        self.rust = self.tree({'rust/src/lib.rs': 'fn api() {}'})
        self.binaries = self.tree({'bin/test_basic': b'original executable'})
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
            transpile=TranspileConfig(config_path='crisp.toml', output_dir='rust', artifacts=[
                TranspileArtifactConfig(config_path='crisp.toml', name='observed',
                                       build_cmds=[], system_libs=['m', 'dl', 'm'])]))

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

    def checkout(self, tree, path):
        self.checkouts[-1].append((tree.node_id(), path))

    def sandbox_run(self, cmd, stream=False):
        self.commands.append(cmd)
        if cmd == ['test', '-f', 'test_results/results.json']:
            return (0 if self.have_results else 1), b''
        if cmd[0] == 'build' or cmd[1:2] == ['build']:
            self.builds += 1
            if self.build_exit_code is not None:
                return self.build_exit_code, b'compile failure logs'
        else:
            self.runs += 1
            rust = next(node_id for node_id, path in self.checkouts[-1] if path == 'rust_code')
            if rust in self.exit_codes_by_rust:
                return self.exit_codes_by_rust[rust], b'run failure logs'
        return self.exit_code, b'runner logs'

    def commit_dir(self, path):
        if path == 'test_binaries':
            return self.tree({'test_binaries/bin/test_basic': b'original executable'})
        rust = next(node_id for node_id, path in self.checkouts[-1] if path == 'rust_code')
        if rust in self.output_files_by_rust:
            return self.tree({'test_results/' + name: body
                              for name, body in self.output_files_by_rust[rust].items()})
        return self.tree({'test_results/results.json': '{"tests": []}',
                          'test_results/tests/basic.c.stdout': b'\x00\xffobservation',
                          'test_results/tests/basic.c.stderr': b'',
                          'test_results/tests/basic.c.json': '{"returncode": 0, "status": "exited"}'})

    def compile(self, c_code=None, tests=None, rust=None, cmd=None):
        return analysis.compile_tests(
            None, self.mvir, c_code or self.c_code, tests or self.tests,
            rust or self.rust, cmd or ['build'])

    def observe(self, rust=None, tests=None, binaries=None, cmd=None, trial=0):
        return analysis.run_observation_tests(
            None, self.mvir, rust or self.rust, tests or self.tests,
            binaries or self.binaries, cmd or ['run'], trial=trial)

    def candidate(self, extra=None):
        return TreeNode.new(self.mvir, files={
            **self.rust.files, **self.tests.files,
            **{name: FileNode.new(self.mvir, body).node_id()
               for name, body in (extra or {}).items()}})

    def compare(self, new_code=None, old_code=None, c_code=None):
        return analysis.compare_observation_tests(
            self.cfg, self.mvir, old_code or self.rust,
            new_code or self.candidate(), c_code or self.c_code)

    def test_compile_is_cached_by_all_input_nodes_and_command(self):
        original = self.compile()
        self.assertEqual(self.compile().node_id(), original.node_id())
        self.assertEqual(self.builds, 1)
        self.compile(c_code=self.tree({'api.h': 'int api(int);'}))
        self.compile(tests=self.tree({'tests/basic.c': 'changed test'}))
        self.compile(rust=self.tree({'rust/src/lib.rs': 'changed baseline'}))
        self.compile(cmd=['build', '--sanitize'])
        self.assertEqual(self.builds, 5)
        self.assertEqual(self.checkouts[0], [
            (self.c_code.node_id(), 'c_code'), (self.tests.node_id(), 'test_code'),
            (self.rust.node_id(), 'rust_code')])
        binaries = self.mvir.node(original.test_binaries)
        self.assertEqual(set(binaries.files), {'bin/test_basic'})
        self.assertEqual(original.body(), b'runner logs')

    def test_run_is_cached_by_all_input_nodes_command_and_trial(self):
        original = self.observe()
        self.assertEqual(self.observe().node_id(), original.node_id())
        self.assertEqual(self.runs, 1)
        self.observe(rust=self.tree({'rust/src/lib.rs': 'changed candidate'}))
        self.observe(tests=self.tree({'tests/basic.c': 'changed test'}))
        self.observe(binaries=self.tree({'bin/test_basic': 'changed executable'}))
        self.observe(cmd=['run', '--test-timeout', '10'])
        repeated = self.observe(trial=1)
        self.assertEqual(self.runs, 6)
        self.assertNotEqual(original.node_id(), repeated.node_id())
        self.assertEqual(self.checkouts[0], [
            (self.rust.node_id(), 'rust_code'), (self.tests.node_id(), 'test_code'),
            (self.binaries.node_id(), 'test_binaries')])
        outputs = self.mvir.node(original.outputs)
        self.assertEqual(set(outputs.files), {
            'results.json', 'tests/basic.c.stdout', 'tests/basic.c.stderr', 'tests/basic.c.json'})
        self.assertEqual(self.mvir.node(outputs.files['tests/basic.c.stdout']).body(),
                         b'\x00\xffobservation')

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
        failed_build = self.observe(trial=1)
        self.assertIsNone(failed_build.outputs)

    def test_comparison_extracts_only_current_suite_and_uses_two_sandboxes(self):
        suite_files = {
            'tests.toml': 'cflags = []', 'tests/basic.c': 'current test',
            'test_helpers/common.c': 'helper', 'test_helpers/common.h': 'header',
            'fixtures/nested/input.txt': 'fixture'}
        candidate = self.candidate({**suite_files, 'README': 'excluded',
                                    'tests.toml.bak': 'excluded', 'fixtures.bak/a': 'excluded',
                                    'rust/tests/internal.rs': 'excluded'})
        old = self.tree({**{'rust/src/lib.rs': 'original Rust'},
                         'tests/obsolete.c': 'obsolete test', 'fixtures/old.txt': 'old fixture'})
        result = self.compare(candidate, old_code=old)
        self.assertTrue(result.passed, result.body_str())
        suite = self.mvir.node(result.test_code)
        self.assertEqual(set(suite.files), set(suite_files))
        self.assertEqual({name: self.mvir.node(node_id).body_str()
                          for name, node_id in suite.files.items()}, suite_files)
        reference = self.mvir.node(result.old_run)
        current = self.mvir.node(result.new_run)
        self.assertEqual(reference.outputs, current.outputs)
        self.assertEqual(current.input_binaries, reference.test_binaries)
        self.assertEqual((self.builds, self.runs), (1, 2))
        self.assertEqual(self.checkouts, [
            [(self.c_code.node_id(), 'c_code'), (suite.node_id(), 'test_code'),
             (old.node_id(), 'rust_code')],
            [(self.c_code.node_id(), 'c_code'), (suite.node_id(), 'test_code'),
             (candidate.node_id(), 'rust_code'), (reference.test_binaries, 'test_binaries')]])
        self.assertEqual(reference.build_cmd, [
            'crisp_test_runner.py', 'build', '--c-dir', 'c_code', '--suite-dir', 'test_code',
            '--rust-dir', 'rust_code/rust', '--build-dir', 'test_binaries',
            '--system-lib', 'dl', '--system-lib', 'm'])
        self.assertEqual(current.build_cmd, [])

    def test_comparison_caches_reference_across_rust_edits_and_tracks_suite_inputs(self):
        original = self.compare()
        self.assertEqual(self.compare().node_id(), original.node_id())
        self.assertEqual((self.builds, self.runs), (1, 2))
        changed = self.compare(self.candidate({'rust/src/lib.rs': 'changed Rust'}))
        self.assertEqual(changed.old_run, original.old_run)
        self.assertEqual((self.builds, self.runs), (1, 3))
        for path in ('tests/basic.c', 'tests.toml', 'test_helpers/common.h', 'fixtures/input'):
            changed = self.compare(self.candidate({path: 'changed suite input'}))
            self.assertNotEqual(changed.old_run, original.old_run)
        self.assertEqual((self.builds, self.runs), (5, 11))
        self.compare(c_code=self.tree({'api.h': 'changed header'}))
        self.compare(old_code=self.tree({'rust/src/lib.rs': 'changed original'}))
        # The fake compiler emits identical binaries, so changing the reference
        # Rust alone can also reuse the previous candidate execution.
        self.assertEqual((self.builds, self.runs), (7, 14))

    def test_comparison_build_failure_skips_runs_and_preserves_feedback(self):
        self.build_exit_code = 2
        result = self.compare()
        self.assertFalse(result.passed)
        self.assertIsNone(result.new_run)
        self.assertEqual((self.builds, self.runs), (1, 0))
        self.assertIn('Reference test build/run failed', result.body_str())
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
        self.assertIn('Candidate test run failed', result.body_str())
        self.assertIn('run failure logs', result.body_str())

    def test_comparison_requires_output_snapshot(self):
        self.have_results = False
        result = self.compare()
        self.assertFalse(result.passed)
        self.assertIsNone(result.new_run)
        self.assertEqual(self.runs, 1)

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
        with tempfile.TemporaryDirectory() as directory:
            mvir = MVIR(directory, '.')

            def tree(files):
                return TreeNode.new(mvir, files={
                    name: FileNode.new(mvir, body).node_id() for name, body in files.items()})

            config_path = str(Path(directory) / 'crisp.toml')
            cfg = Config(config_path=config_path, project_name='observed', src_globs=[],
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
                self.assertEqual(current.input_binaries, reference.test_binaries)
                outputs = mvir.node(reference.outputs)
                self.assertEqual(mvir.node(outputs.files['tests/value.c.stdout']).body(),
                                 b'7\nfixture observation\n')
                self.assertEqual(len(workspaces), 2)
                self.assertEqual(invocations, ['build', 'run', 'run'])
                cached = analysis.compare_observation_tests(cfg, mvir, initial, candidate, c_code)
                self.assertEqual(cached.node_id(), result.node_id())
                regression = analysis.compare_observation_tests(cfg, mvir, initial, changed, c_code)
                self.assertFalse(regression.passed)
                self.assertEqual(regression.old_run, reference.node_id())
                self.assertIn('golden-outputs/tests/value.c.stdout', regression.body_str())
                self.assertIn('current-outputs/tests/value.c.stdout', regression.body_str())
                self.assertIn('-7\n+9\n', regression.body_str())
                self.assertEqual(len(workspaces), 3)
                self.assertEqual(invocations, ['build', 'run', 'run', 'run'])

    def test_build_and_run_mvir_bundles_in_separate_workspaces(self):
        runner = Path(__file__).resolve().parents[1] / 'scripts/crisp_test_runner.py'
        with tempfile.TemporaryDirectory() as directory:
            mvir = MVIR(directory, '.')

            def tree(files):
                return TreeNode.new(mvir, files={
                    name: FileNode.new(mvir, body).node_id() for name, body in files.items()})

            c_code = tree({'api.h': 'int value(void);'})
            tests = tree({'tests/value.c': '''
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
                report = json.loads(mvir.node(outputs.files['results.json']).body())
                case = report['tests'][0]
                self.assertNotIn('elapsed_seconds', case)
                metadata = json.loads(mvir.node(outputs.files[case['metadata']]).body())
                self.assertEqual(metadata['returncode'], 0)
                return mvir.node(outputs.files[report['tests'][0]['stdout']]).body()

            with patch('crisp.analysis.run_sandbox', sandbox):
                build_cmd = [sys.executable, str(runner), 'build', '--c-dir', 'c_code',
                             '--suite-dir', 'test_code', '--rust-dir', 'rust_code/rust',
                             '--build-dir', 'test_binaries']
                compiled = analysis.compile_tests(None, mvir, c_code, tests, initial, build_cmd)
                self.assertEqual(compiled.exit_code, 0, compiled.body_str())
                binaries = mvir.node(compiled.test_binaries)
                run_cmd = [sys.executable, str(runner), 'run', '--suite-dir', 'test_code',
                           '--rust-dir', 'rust_code/rust', '--build-dir', 'test_binaries',
                           '--output-dir', 'test_results']
                reference = analysis.run_observation_tests(
                    None, mvir, initial, tests, binaries, run_cmd)
                self.assertEqual(observation(reference), b'7\n')
                cached = analysis.run_observation_tests(
                    None, mvir, initial, tests, binaries, run_cmd)
                self.assertEqual(cached.node_id(), reference.node_id())
                changed = analysis.run_observation_tests(
                    None, mvir, candidate, tests, binaries, run_cmd)
                self.assertEqual(observation(changed), b'9\n')
                repeated = analysis.run_observation_tests(
                    None, mvir, candidate, tests, binaries, run_cmd, trial=1)
                self.assertEqual(observation(repeated), b'9\n')
                self.assertEqual(repeated.outputs, changed.outputs)
                self.assertEqual(len(invocations), 4)


if __name__ == '__main__':
    unittest.main()
