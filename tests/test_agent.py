from contextlib import contextmanager
import tempfile
from types import SimpleNamespace
import tomllib
import unittest
from unittest.mock import patch

from crisp import agent, analysis, prompts
from crisp.config import ModelsConfig
from crisp.error import AgentQuotaError, CrispError
from crisp.mvir import FileNode, MVIR, TreeNode


class CodexAgentProfilesTest(unittest.TestCase):
    def test_planning_profiles_use_current_codex_schema(self):
        expected_models = {
            'ffi_abi_analyst': ('gpt-6.1-sol', 'high'),
            'ownership_analyst': ('gpt-6.1-sol', 'xhigh'),
            'collections_analyst': ('gpt-6.1-sol', 'high'),
            'strings_analyst': ('gpt-6.1-sol', 'medium'),
            'libc_analyst': ('gpt-6.1-sol', 'medium'),
            'macro_analyst': ('gpt-6.1-sol', 'medium'),
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
        written = {}

        def capture(_sb, _mvir, rel_path, body):
            written[rel_path] = body

        with patch.object(agent, '_checkout_bytes', side_effect=capture):
            agent._inject_codex_agents(
                object(), object(), agent.PLANNING_CODEX_AGENTS)

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
            agent._inject_codex_agents(object(), object(), ('missing',))

    def test_planning_prompt_orchestrates_all_profiles(self):
        prompt = prompts.agent_plan('translated_rust')
        for name in agent.PLANNING_CODEX_AGENTS:
            self.assertIn(f'`{name}`', prompt)
        self.assertIn('fork_turns="none"', prompt)
        self.assertIn('Wait for all agents', prompt)
        self.assertIn('only the parent agent write', prompt)
        self.assertIn('`translated_rust`', prompt)
        self.assertIn('$FIND_UNSAFE2_JSON_DIR', prompt)
        self.assertIn('.codex/safety_constraints.md', prompt)
        self.assertIn('Do not modify, create, rename, or delete',
                      prompt)
        # The required plan sections.
        self.assertIn('## FFI entry point rules', prompt)
        self.assertIn('## Conventions', prompt)
        self.assertIn('## Cluster guide', prompt)
        self.assertIn('## Status', prompt)
        # The plan must not carry verification commands; the harness does.
        self.assertIn('the harness supplies all validation', prompt)


class QuotaErrorDetectionTest(unittest.TestCase):
    def test_spend_cap_error_is_detected(self):
        logs = (b'tokens used\n'
            b'ERROR: You hit your spend cap set in your workspace. '
            b'Increase your spend cap to continue.\n')
        self.assertTrue(agent._is_quota_error(logs))

    def test_marker_outside_error_line_is_ignored(self):
        # Agent logs include file contents and command output, which may
        # mention the same words.
        logs = b'cat notes.md\nYou hit your spend cap if you run too long.\n'
        self.assertFalse(agent._is_quota_error(logs))

    def test_other_errors_are_not_quota_errors(self):
        logs = b'ERROR: stream disconnected before completion\n'
        self.assertFalse(agent._is_quota_error(logs))

    def test_json_error_events_are_detected(self):
        # `codex exec review --json` output from a real spend-cap failure.
        logs = (b'{"type":"turn.started"}\n'
            b'{"type":"error","message":"You hit your spend cap set in your '
            b'workspace. Increase your spend cap to continue."}\n')
        self.assertTrue(agent._is_quota_error(logs))
        # The same words in other JSON events (e.g. agent output) don't count.
        logs = (b'{"type":"item.completed","item":{"type":"agent_message",'
            b'"text":"You hit your spend cap if you run too long."}}\n')
        self.assertFalse(agent._is_quota_error(logs))


class CodexCommandTest(unittest.TestCase):
    def test_login_uses_model_alias(self):
        cmd = agent._codex_command('exec', ['prompt'], 'gpt-5.4-2026-03-05',
            use_login = True)
        self.assertEqual(cmd[cmd.index('--model') + 1], 'gpt-5.4')
        self.assertNotIn('model_provider=crisp', cmd)

    def test_api_key_uses_crisp_provider(self):
        cmd = agent._codex_command('exec', ['prompt'], 'gpt-5.4-2026-03-05',
            use_login = False)
        self.assertNotIn('--model', cmd)
        self.assertIn('model_provider=crisp', cmd)
        self.assertIn('model_providers.crisp.env_key=CRISP_API_KEY', cmd)


LOGIN_QUOTA = (1, b'ERROR: You hit your spend cap set in your workspace.\n')
API_QUOTA = (1, b'ERROR: exceeded your current quota\n')


class _FakeSandbox:
    """Runs no commands; each `codex` command returns the next result."""
    def __init__(self, mvir, results):
        self.mvir = mvir
        self.results = list(results)
        self.cmds = []

    def join(self, path):
        return '/sandbox/' + path

    def checkout(self, *args, **kwargs):
        pass

    checkout_file = checkout_file_untracked = checkout

    def run(self, cmd, **kwargs):
        self.cmds.append(cmd)
        return self.results.pop(0) if cmd[0] == 'codex' else (0, b'')

    def commit_dir(self, *args, **kwargs):
        return TreeNode.new(self.mvir, files = {})


class CodexFallbackTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mvir = MVIR(tmp.name, '.')
        # Each test starts with the login usable and a fake clock.
        self.now = 0.0
        for p in (
            patch.object(agent, '_codex_auth_input',
                lambda: agent.Input(b'{}', path = '.codex/auth.json')),
            patch('builtins.print'),
            patch.object(agent, '_login_retry_at', 0.0),
            patch.object(agent, 'time', SimpleNamespace(monotonic = lambda: self.now)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def run_codex(self, results, fallback = True, **kwargs):
        self.sb = _FakeSandbox(self.mvir, results)
        @contextmanager
        def run_sandbox(cfg, mvir):
            yield self.sb
        cfg = SimpleNamespace(codex_login = True, codex_login_fallback = fallback)
        with patch.object(agent, 'run_sandbox', run_sandbox):
            return agent._run_codex(cfg, self.mvir,
                {'code': agent.Input(TreeNode.new(self.mvir, files = {}))},
                ['prompt'], 'gpt-x', 'high', {}, **kwargs)

    def codex_runs(self):
        return ['login' if '--model' in c else 'api_key'
            for c in self.sb.cmds if c[0] == 'codex']

    def last_reason(self):
        return self.mvir.tag_reflog('op_history')[-1].reason

    def test_login_quota_error_reruns_in_the_same_sandbox(self):
        n_op, _ = self.run_codex([LOGIN_QUOTA, (0, b'done')], op_label = 'c_code_docs')
        self.assertEqual(self.codex_runs(), ['login', 'api_key'])
        self.assertEqual(n_op.exit_code, 0)
        # The checkout is reset to the inputs before the rerun.
        i = self.sb.cmds.index(['git', 'reset', '--hard', '--quiet'])
        self.assertEqual(self.sb.cmds[i + 2][0], 'codex')
        self.assertEqual(self.last_reason(), 'codex_agent_op_v3 c_code_docs api_key_fallback')

    def test_without_git_nothing_is_reset(self):
        self.run_codex([LOGIN_QUOTA, (0, b'done')], init_git = False)
        self.assertEqual(self.codex_runs(), ['login', 'api_key'])
        self.assertFalse(any(c[0] == 'git' for c in self.sb.cmds))

    def test_after_a_quota_error_runs_skip_the_login(self):
        self.run_codex([LOGIN_QUOTA, (0, b'done')])
        self.now = agent._LOGIN_RETRY_DELAY - 1
        self.run_codex([(0, b'done')])
        self.assertEqual(self.codex_runs(), ['api_key'])
        self.assertFalse(any(c[0] == 'git' and c[1] == 'reset' for c in self.sb.cmds))
        self.assertEqual(self.last_reason(), 'codex_agent_op_v3 api_key_fallback')

    def test_login_is_tried_again_after_the_delay(self):
        self.run_codex([LOGIN_QUOTA, (0, b'done')])
        self.now = agent._LOGIN_RETRY_DELAY
        self.run_codex([LOGIN_QUOTA, (0, b'done')])
        self.assertEqual(self.codex_runs(), ['login', 'api_key'])
        # The new quota error starts a new delay.
        self.now = 2 * agent._LOGIN_RETRY_DELAY - 1
        self.run_codex([(0, b'done')])
        self.assertEqual(self.codex_runs(), ['api_key'])
        self.now = 2 * agent._LOGIN_RETRY_DELAY
        self.run_codex([(0, b'done')])
        self.assertEqual(self.codex_runs(), ['login'])
        self.assertEqual(self.last_reason(), 'codex_agent_op_v3')

    def test_without_fallback_the_login_is_always_tried(self):
        # Even right after a run with the fallback hit a quota error.
        self.run_codex([LOGIN_QUOTA, (0, b'done')])
        with self.assertRaises(AgentQuotaError):
            self.run_codex([LOGIN_QUOTA], fallback = False)
        self.assertEqual(self.codex_runs(), ['login'])

    def test_api_key_quota_error_stops(self):
        with self.assertRaises(AgentQuotaError):
            self.run_codex([LOGIN_QUOTA, API_QUOTA])

    def test_other_api_key_errors_are_not_quota_errors(self):
        with self.assertRaises(CrispError) as cm:
            self.run_codex([LOGIN_QUOTA, (1, b'ERROR: stream disconnected\n')])
        self.assertNotIsInstance(cm.exception, AgentQuotaError)

    def test_without_fallback_login_quota_error_stops(self):
        with self.assertRaises(AgentQuotaError):
            self.run_codex([LOGIN_QUOTA], fallback = False)
        self.assertEqual(self.codex_runs(), ['login'])

    def test_other_errors_do_not_fall_back(self):
        with self.assertRaises(CrispError):
            self.run_codex([(1, b'ERROR: stream disconnected\n')])
        self.assertEqual(self.codex_runs(), ['login'])


class FfiReviewModelTest(unittest.TestCase):
    def review_model(self, **models):
        """Run `ffi_review` up to the codex call and return the model it used."""
        class Stop(Exception):
            pass
        used = []
        def run_review_op(cfg, mvir, prompt, model, old_code, new_code):
            used.append(model)
            raise Stop()
        cfg = SimpleNamespace(
            models = ModelsConfig(**models),
            transpile = SimpleNamespace(output_dir = 'rust'),
            relative_path = lambda path: path,
        )
        with tempfile.TemporaryDirectory() as storage:
            mvir = MVIR(storage, '.')
            old_code = TreeNode.new(mvir, files = {})
            new_code = TreeNode.new(mvir, files = {'a.rs': FileNode.new(mvir, 'a').node_id()})
            prompt = FileNode.new(mvir, 'review')
            with patch.object(agent, 'run_review_op', run_review_op), \
                    self.assertRaises(Stop):
                analysis.ffi_review(cfg, mvir, old_code, new_code, prompt)
        return used[0]

    def test_review_defaults_to_agent_loop(self):
        self.assertEqual(self.review_model(agent_loop = 'loop'), 'loop')

    def test_agent_review_overrides_agent_loop(self):
        self.assertEqual(
            self.review_model(agent_loop = 'loop', agent_review = 'review'),
            'review')


if __name__ == '__main__':
    unittest.main()
