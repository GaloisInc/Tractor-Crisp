import tomllib
import unittest
from unittest.mock import patch

from crisp import agent, prompts
from crisp.error import CrispError


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


if __name__ == '__main__':
    unittest.main()
