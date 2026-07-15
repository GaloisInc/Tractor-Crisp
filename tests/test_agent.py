import tomllib
import unittest
from unittest.mock import patch

from crisp import agent
from crisp.error import CrispError
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


if __name__ == '__main__':
    unittest.main()
