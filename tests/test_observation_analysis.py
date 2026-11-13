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
        if cmd[0] == 'build':
            self.builds += 1
        else:
            self.runs += 1
        return self.exit_code, b'runner logs'

    def commit_dir(self, path):
        if path == 'test_binaries':
            return self.tree({'test_binaries/bin/test_basic': b'original executable'})
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


@unittest.skipUnless(shutil.which('cargo') and shutil.which('clang'), 'requires cargo and clang')
class ObservationAnalysisIntegrationTest(unittest.TestCase):
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
