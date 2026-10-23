from datetime import datetime, timedelta
import json
import tempfile
from types import SimpleNamespace
import tomllib
import unittest
from unittest.mock import patch

from crisp import agent, prompts
from crisp.error import AgentQuotaError, CrispError
from crisp.mvir import FileNode, MVIR, TreeNode
from crisp.workflow import describe_c_sources


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
        for name in agent.PLANNING_CODEX_AGENTS:
            self.assertIn(f'`{name}`', prompts.AGENT_PLAN)
        self.assertIn('fork_turns="none"', prompts.AGENT_PLAN)
        self.assertIn('Wait for all agents', prompts.AGENT_PLAN)
        self.assertIn('only the parent agent write', prompts.AGENT_PLAN)
        self.assertIn('`{cargo_dir_path}`', prompts.AGENT_PLAN)
        self.assertIn('$FIND_UNSAFE2_JSON_DIR', prompts.AGENT_PLAN)
        self.assertIn('.codex/safety_constraints.md', prompts.AGENT_PLAN)
        self.assertIn('Do not modify, create, rename, or delete',
                      prompts.AGENT_PLAN)
        # The required plan sections.
        self.assertIn('## FFI entry point rules', prompts.AGENT_PLAN)
        self.assertIn('## Conventions', prompts.AGENT_PLAN)
        self.assertIn('## Cluster guide', prompts.AGENT_PLAN)
        self.assertIn('## Status', prompts.AGENT_PLAN)
        # The plan must not carry verification commands; the harness does.
        self.assertIn('the harness supplies all validation', prompts.AGENT_PLAN)
        # The planner is pointed at the C code docs.
        self.assertIn('`{c_docs_dir}/README.md`', prompts.AGENT_PLAN)
        # The planner gets the C sources as well as the docs.
        self.assertNotIn('not in the workspace', prompts.AGENT_PLAN)

    def test_c_code_docs_prompt_formats(self):
        prompt = prompts.AGENT_C_CODE_DOCS.format(
            docs_dir = agent.C_DOCS_DIR,
            c_sources = '`lib/a.c`, `lib/a.h`')
        self.assertIn(f'`{agent.C_DOCS_DIR}/README.md`', prompt)
        self.assertIn(f'`{agent.C_DOCS_DIR}/TESTING.md`', prompt)
        self.assertIn('`lib/a.c`, `lib/a.h`', prompt)

    def test_describe_c_sources(self):
        with tempfile.TemporaryDirectory() as storage:
            mvir = MVIR(storage, '.')
            empty = FileNode.new(mvir, '').node_id()
            paths = ['zlib.h', 'deflate.c', 'Makefile', 'test/example.c'] + \
                [f'contrib/x{i}.c' for i in range(3)]
            tree = TreeNode.new(mvir, files = {p: empty for p in paths})
            # Root files first and listed by name; non-C files left out; big
            # directories counted.
            self.assertEqual(describe_c_sources(tree, max_listed = 2),
                '`deflate.c`, `zlib.h`; 3 files under `contrib/`; `test/example.c`')
            no_c = TreeNode.new(mvir, files = {'Makefile': empty})
            self.assertEqual(describe_c_sources(no_c), 'none found')

    def test_rust_reimpl_plan_prompt_formats(self):
        prompt = prompts.AGENT_RUST_REIMPL_PLAN.format(
            plan_path = agent.RUST_REIMPL_PLAN_PATH,
            sources = '- SOURCE LIST',
            ffi_entry_point_rules = 'FFI RULES')
        self.assertIn(f'`{agent.RUST_REIMPL_PLAN_PATH}`', prompt)
        self.assertIn('- SOURCE LIST', prompt)
        # The migration section is planned under the safety loop's FFI rules.
        self.assertIn('FFI RULES', prompt)
        self.assertIn('## C ABI shim', prompt)
        self.assertIn('## Milestones', prompt)
        # The plan is also the safety migration's target design.
        self.assertIn('## Migration from the C2Rust translation', prompt)
        # The safety planner is pointed at the section written for it.
        self.assertIn('## Migration from the C2Rust translation', prompts.AGENT_PLAN)
        # The plan lives with the docs, so it carries over with them.
        self.assertTrue(agent.RUST_REIMPL_PLAN_PATH.startswith(agent.C_DOCS_DIR + '/'))
        self.assertIn('`{rust_reimpl_plan_path}`', prompts.AGENT_PLAN)
        self.assertIn('# The reimplementation plan as target', prompts.AGENT_PLAN)


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


def _session(*rate_limits):
    # One codex `token_count` event per `rate_limits` report, as in real
    # session logs.
    return '\n'.join(json.dumps({'type': 'event_msg', 'payload': {
        'type': 'token_count', 'info': None, 'rate_limits': rl}})
        for rl in rate_limits).encode()

def _window(used_percent, resets_at):
    return {'used_percent': used_percent, 'window_minutes': 300,
        'resets_at': resets_at}


class SessionQuotaStateTest(unittest.TestCase):
    def test_under_limits(self):
        session = _session({'limit_id': 'codex',
            'primary': _window(14.0, 1792813819),
            'secondary': _window(18.0, 1793382528),
            'rate_limit_reached_type': None})
        self.assertEqual(agent._session_quota_state(session), (False, None))

    def test_workspace_spend_cap_has_no_reset_time(self):
        # As seen in a real run: an earlier report under the limits, then the
        # owner-set limit with no windows.
        session = _session(
            {'limit_id': 'codex', 'primary': _window(82.0, 1792746682),
                'secondary': None, 'rate_limit_reached_type': None},
            {'limit_id': 'premium', 'primary': None, 'secondary': None,
                'rate_limit_reached_type': 'workspace_owner_usage_limit_reached'})
        self.assertEqual(agent._session_quota_state(session), (True, None))

    def test_full_windows_report_the_latest_reset(self):
        session = _session({'limit_id': 'codex',
            'primary': _window(100.0, 1792813819),
            'secondary': _window(100.0, 1793382528),
            'rate_limit_reached_type': None})
        self.assertEqual(agent._session_quota_state(session),
            (True, datetime.fromtimestamp(1793382528)))

    def test_missing_or_malformed_session(self):
        self.assertEqual(agent._session_quota_state(b''), (False, None))
        self.assertEqual(agent._session_quota_state(b'not json\n[1, 2]\n'), (False, None))


def _fallback_cfg(**kwargs):
    defaults = dict(codex_login = True, codex_login_fallback = True,
        codex_login_probe_minutes = 30)
    return SimpleNamespace(**{**defaults, **kwargs})


class _Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 23, 12, 0)

    def __call__(self):
        return self.t


class CodexAuthTest(unittest.TestCase):
    def test_known_reset_time(self):
        clock = _Clock()
        auth = agent.CodexAuth(now = clock)
        cfg = _fallback_cfg()
        self.assertTrue(auth.use_login(cfg))
        auth.mark_login_exhausted(cfg, clock.t + timedelta(hours = 2))
        clock.t += timedelta(hours = 1)
        self.assertFalse(auth.use_login(cfg))
        clock.t += timedelta(hours = 1)
        self.assertTrue(auth.use_login(cfg))
        self.assertIsNone(auth.login_unavailable_until)

    def test_unknown_or_past_reset_time_probes_after_interval(self):
        clock = _Clock()
        auth = agent.CodexAuth(now = clock)
        cfg = _fallback_cfg(codex_login_probe_minutes = 10)
        for resets_at in (None, clock.t - timedelta(minutes = 1)):
            auth.mark_login_exhausted(cfg, resets_at)
            self.assertEqual(auth.login_unavailable_until,
                clock.t + timedelta(minutes = 10))

    def test_without_codex_login(self):
        auth = agent.CodexAuth(now = _Clock())
        self.assertFalse(auth.use_login(_fallback_cfg(codex_login = False)))

    def test_unknown_reset_uses_earlier_window_reset(self):
        clock = _Clock()
        auth = agent.CodexAuth(now = clock)
        cfg = _fallback_cfg(codex_login_probe_minutes = 30)
        past, soon, later = (clock.t + timedelta(minutes = m) for m in (-5, 11, 300))
        auth.note_window_resets([past, later, soon])
        auth.mark_login_exhausted(cfg, None)
        self.assertEqual(auth.login_unavailable_until, soon)
        # A window reset after the probe time doesn't delay the probe.
        auth.note_window_resets([later])
        auth.mark_login_exhausted(cfg, None)
        self.assertEqual(auth.login_unavailable_until, clock.t + timedelta(minutes = 30))


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


class CodexFallbackTest(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.auth = agent.CodexAuth(now = self.clock)
        self.calls = []
        self.results = []
        patches = [
            patch.object(agent, 'CODEX_AUTH', self.auth),
            patch.object(agent, '_codex_auth_input',
                lambda: agent.Input(b'{}', path = '.codex/auth.json')),
            patch.object(agent, 'run_agent', self.fake_run_agent),
            patch.object(agent, '_op_window_resets', lambda mvir, n_op: self.window_resets),
        ]
        self.window_resets = []
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_run_agent(self, cfg, mvir, inputs, codex_cmd, output_filters, **kwargs):
        self.calls.append(dict(login = 'codex_auth' in inputs,
            cmd = codex_cmd, op_label = kwargs.get('op_label')))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def run_codex(self, cfg, op_label = 'c_code_docs'):
        return agent._run_codex(cfg, None, {'code': agent.Input(b'')},
            ['prompt'], 'gpt-x', 'high', {}, op_label = op_label)

    def test_quota_error_reruns_with_api_key_until_reset(self):
        reset = self.clock.t + timedelta(hours = 2)
        self.results = [AgentQuotaError('quota', resets_at = reset), 'api', 'api', 'login']
        cfg = _fallback_cfg()
        self.assertEqual(self.run_codex(cfg), 'api')
        self.assertEqual([c['login'] for c in self.calls], [True, False])
        self.assertEqual(self.calls[1]['op_label'], 'c_code_docs api_key_fallback')
        self.assertIn('model_provider=crisp', self.calls[1]['cmd'])

        # Before the reset, runs go straight to the API key.
        self.clock.t += timedelta(hours = 1)
        self.assertEqual(self.run_codex(cfg, op_label = None), 'api')
        self.assertEqual(self.calls[2]['login'], False)
        self.assertEqual(self.calls[2]['op_label'], 'api_key_fallback')

        # After the reset, back to the login.
        self.clock.t += timedelta(hours = 1)
        self.assertEqual(self.run_codex(cfg), 'login')
        self.assertEqual(self.calls[3]['login'], True)
        self.assertEqual(self.calls[3]['op_label'], 'c_code_docs')

    def test_successful_login_run_records_window_resets(self):
        reset = self.clock.t + timedelta(hours = 5)
        self.window_resets = [reset]
        self.results = ['login']
        self.run_codex(_fallback_cfg())
        self.assertEqual(self.auth.window_resets, [reset])

    def test_without_fallback_quota_error_propagates(self):
        self.results = [AgentQuotaError('quota')]
        with self.assertRaises(AgentQuotaError):
            self.run_codex(_fallback_cfg(codex_login_fallback = False))
        self.assertEqual(len(self.calls), 1)

    def test_api_key_quota_error_propagates(self):
        self.results = [AgentQuotaError('login quota'), AgentQuotaError('api quota')]
        with self.assertRaisesRegex(AgentQuotaError, 'api quota'):
            self.run_codex(_fallback_cfg())
        self.assertEqual([c['login'] for c in self.calls], [True, False])

    def test_other_errors_do_not_fall_back(self):
        self.results = [CrispError('agent failed')]
        with self.assertRaises(CrispError):
            self.run_codex(_fallback_cfg())
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.auth.login_unavailable_until)


if __name__ == '__main__':
    unittest.main()
