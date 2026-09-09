from contextlib import nullcontext
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

from crisp import agent
from crisp.error import CrispError
from crisp.mvir import FileNode, MVIR, TreeNode
from crisp.workflow import AGENT_PLAN_PROMPT


class CodexAgentProfilesTest(unittest.TestCase):
    def test_planning_profiles_use_current_codex_schema(self):
        expected_models = {
            'ffi_abi_analyst': ('gpt-5.6-sol', 'high'),
            'ownership_analyst': ('gpt-5.6-sol', 'xhigh'),
            'collections_analyst': ('gpt-5.6-terra', 'high'),
            'strings_analyst': ('gpt-5.6-terra', 'medium'),
            'libc_analyst': ('gpt-5.6-terra', 'medium'),
            'macro_analyst': ('gpt-5.6-terra', 'medium'),
        }
        profiles = {
            path.stem: tomllib.loads(path.read_text())
            for path in agent._CODEX_ASSET_DIR.glob('*.toml')
        }

        self.assertEqual(set(profiles), set(agent.PLANNING_CODEX_AGENTS))
        for name, profile in profiles.items():
            self.assertEqual(profile['name'], name)
            self.assertTrue(profile['description'])
            self.assertTrue(profile['developer_instructions'])
            self.assertEqual(profile['sandbox_mode'], 'read-only')
            self.assertEqual(
                (profile['model'], profile['model_reasoning_effort']),
                expected_models[name],
            )
            self.assertIn(
                '.codex/safety_constraints.md',
                profile['developer_instructions'],
            )

    def test_planning_profiles_are_injected_under_codex_home(self):
        inputs = {}
        agent._add_codex_agent_inputs(inputs, agent.PLANNING_CODEX_AGENTS)
        written = {item.path: item.item for item in inputs.values()}

        self.assertIn('.codex/safety_constraints.md', written)
        self.assertEqual(
            {
                path.removeprefix('.codex/agents/').removesuffix('.toml')
                for path in written
                if path.startswith('.codex/agents/')
            },
            set(agent.PLANNING_CODEX_AGENTS),
        )

    def test_unknown_profile_is_rejected(self):
        with self.assertRaisesRegex(CrispError, 'unknown Codex agent profile'):
            agent._add_codex_agent_inputs({}, ('missing',))

    def test_planning_prompt_orchestrates_all_profiles(self):
        for name in agent.PLANNING_CODEX_AGENTS:
            self.assertIn(f'`{name}`', AGENT_PLAN_PROMPT)
        self.assertIn('fork_turns="none"', AGENT_PLAN_PROMPT)
        self.assertIn('Wait for all agents', AGENT_PLAN_PROMPT)
        self.assertIn('only the parent agent write', AGENT_PLAN_PROMPT)
        self.assertIn('`{cargo_dir_path}`', AGENT_PLAN_PROMPT)
        self.assertIn('$FIND_UNSAFE2_JSON_DIR', AGENT_PLAN_PROMPT)
        self.assertIn('.codex/safety_constraints.md', AGENT_PLAN_PROMPT)
        self.assertIn('Do not modify, create, rename, or delete',
                      AGENT_PLAN_PROMPT)
        # The required plan sections.
        self.assertIn('## FFI entry point rules', AGENT_PLAN_PROMPT)
        self.assertIn('## Conventions', AGENT_PLAN_PROMPT)
        self.assertIn('## Cluster guide', AGENT_PLAN_PROMPT)
        self.assertIn('## Status', AGENT_PLAN_PROMPT)
        # The plan must not carry verification commands; the harness does.
        self.assertIn('the harness supplies all validation', AGENT_PLAN_PROMPT)


class AgentExecutionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.mvir = MVIR(temp.name, temp.name)
        self.code = TreeNode.new(self.mvir, files={
            'crate/src/lib.rs': FileNode.new(self.mvir, 'fn example() {}').node_id(),
        })
        self.files = {
            'crate/src/lib.rs': b'fn example() {}',
            '.codex/last_message.txt': b'TARGET: example\nDONE',
            '.codex/auth.json': b'ignored test credentials',
        }
        self.sb = Mock()
        self.sb.join.side_effect = lambda *paths: os.path.join('/sandbox', *paths)
        self.sb.run.return_value = (0, b'{"type":"item.completed","item":'
            b'{"type":"command_execution","exit_code":0}}')

        def commit_dir(_path, ignore_spec):
            return TreeNode.new(self.mvir, files={
                path: FileNode.new(self.mvir, body).node_id()
                for path, body in self.files.items()
                if not ignore_spec.match_file(path)
            })

        self.sb.commit_dir.side_effect = commit_dir
        self.enterContext(patch.object(agent, 'run_sandbox',
            return_value=nullcontext(self.sb)))
        self.enterContext(patch('builtins.print'))

    def test_rewrite_preserves_final_message_through_output_filter(self):
        for cwd in ('.', 'crate'):
            with self.subTest(cwd=cwd):
                code, plans, message = agent.run_rewrite(
                    object(), self.mvir, 'rewrite', 'test-model', self.code,
                    cwd=cwd, effort='medium')
                self.assertEqual(code.files, self.code.files)
                self.assertEqual(plans.files, {})
                self.assertEqual(message, 'TARGET: example\nDONE')
                cmd = next(call.args[0] for call in reversed(self.sb.run.call_args_list)
                    if call.args[0][0] == 'codex')
                output_path = cmd[cmd.index('--output-last-message') + 1]
                self.assertEqual(os.path.normpath(os.path.join(cwd, output_path)),
                    '.codex/last_message.txt')
                self.assertIn('model_reasoning_effort="medium"', cmd)

    def test_missing_rewrite_message_is_empty(self):
        del self.files['.codex/last_message.txt']
        _, _, message = agent.run_rewrite(
            object(), self.mvir, 'rewrite', 'test-model', self.code)
        self.assertEqual(message, '')

    def test_review_preserves_report_and_command_evidence(self):
        self.files['.codex/last_message.txt'] = b'No findings.'
        report, _, ran_commands = agent.run_review(
            object(), self.mvir, 'review', 'test-model', self.code, self.code,
            cwd='crate', effort='xhigh')
        self.assertEqual(report, 'No findings.')
        self.assertTrue(ran_commands)

    def test_missing_review_report_reaches_the_fail_closed_gate(self):
        del self.files['.codex/last_message.txt']
        report, _, _ = agent.run_review(
            object(), self.mvir, 'review', 'test-model', self.code, self.code)
        self.assertEqual(report, '')

    def test_review_reuses_original_paths_without_changing_the_candidate_diff(self):
        reference = TreeNode.new(self.mvir, files={
            'api.h': FileNode.new(self.mvir, 'int example(void);').node_id(),
        })
        candidate = TreeNode.new(self.mvir, files={
            'crate/src/lib.rs': FileNode.new(self.mvir, 'fn changed() {}').node_id(),
        })
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))

        def write_file(path, body):
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)

        def checkout(tree, rel_path):
            for path, node_id in tree.files.items():
                write_file(str(Path(rel_path) / path), self.mvir.node(node_id).body())

        def run(cmd, **kwargs):
            if cmd[0] == 'codex':
                def git(*args):
                    return subprocess.check_output(['git', *args], cwd=root).decode()
                self.assertEqual(git('show', 'HEAD:api.h'), 'int example(void);')
                self.assertEqual((root / 'api.h').read_text(), 'int example(void);')
                self.assertEqual(git('diff', '--name-only', 'HEAD'), 'crate/src/lib.rs\n')
                self.assertNotIn('api.h',
                    git('ls-files', '--others', '--exclude-standard').splitlines())
                self.assertFalse((root / 'crisp_old_code').exists())
                return 0, b'{"type":"item.completed","item":' \
                    b'{"type":"command_execution","exit_code":0}}'
            result = subprocess.run(cmd, cwd=root, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            return result.returncode, result.stdout

        self.sb.checkout.side_effect = checkout
        self.sb.checkout_file_untracked.side_effect = write_file
        self.sb.join.side_effect = lambda *paths: str(root.joinpath(*paths))
        self.sb.run.side_effect = run
        self.files['.codex/last_message.txt'] = b'CRISP_REVIEW: PASS\nChecked API.'
        report, _, _ = agent.run_review(object(), self.mvir, 'review',
            'test-model', self.code, candidate, extra_code={'c_code': reference})
        self.assertTrue(report.startswith('CRISP_REVIEW: PASS'))
        self.assertEqual(sum(call.args[0] == reference
            for call in self.sb.checkout.call_args_list), 1)
        self.sb.checkout.assert_any_call(reference, rel_path='.')

    def test_review_context_cannot_overwrite_candidate_files(self):
        with self.assertRaisesRegex(CrispError, 'review context overlaps'):
            agent.run_review(object(), self.mvir, 'review', 'test-model',
                self.code, self.code, extra_code={'c_code': self.code})


if __name__ == '__main__':
    unittest.main()
