import tempfile
import tomllib
import unittest
from unittest.mock import patch

from crisp import agent, prompts
from crisp.error import CrispError
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
        # The plan lives with the docs, so it carries over with them.
        self.assertTrue(agent.RUST_REIMPL_PLAN_PATH.startswith(agent.C_DOCS_DIR + '/'))
        self.assertIn('`{rust_reimpl_plan_path}`', prompts.AGENT_PLAN)


if __name__ == '__main__':
    unittest.main()
