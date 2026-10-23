import os
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from crisp import llm
from crisp.__main__ import check_codex_login_fallback, prior_agent_plans
from crisp.mvir import CodexAgentOpNode, FileNode, MVIR, TreeNode


class PlanRecoveryTest(unittest.TestCase):
    def test_current_records_recover_latest_producers_plan(self):
        with tempfile.TemporaryDirectory() as storage:
            mvir = MVIR(storage, '.')
            code = TreeNode.new(mvir, files={})
            plans = TreeNode.new(mvir, files={
                'SAFETY_PLAN.md': FileNode.new(mvir, 'original plan').node_id()})
            revised = TreeNode.new(mvir, files={
                'SAFETY_PLAN.md': FileNode.new(mvir, 'revised plan').node_id()})
            session = FileNode.new(mvir, '').node_id()

            def record(inputs, outputs, exit_code=0):
                op = CodexAgentOpNode.new(mvir, inputs=inputs, outputs=outputs,
                    cmds=[], exit_code=exit_code, raw_output_files=code.node_id(),
                    json_session=session, body='')
                mvir.set_tag('op_history', op.node_id(), op.kind)

            # A reference in inputs or a different output is not a producer.
            record({'code': code.node_id()},
                {'code': revised.node_id(), 'plans': code.node_id()})
            record({}, {'plans': code.node_id()})
            self.assertIsNone(prior_agent_plans(mvir, code))
            record({}, {'code': code.node_id(), 'plans': plans.node_id()})
            self.assertEqual(prior_agent_plans(mvir, code).node_id(), plans.node_id())
            record({}, {'code': code.node_id(), 'plans': revised.node_id()})
            self.assertEqual(prior_agent_plans(mvir, code).node_id(), revised.node_id())

    def test_plans_without_a_plan_or_from_failed_ops_are_ignored(self):
        with tempfile.TemporaryDirectory() as storage:
            mvir = MVIR(storage, '.')
            code = TreeNode.new(mvir, files={})
            docs_only = TreeNode.new(mvir, files={
                'crisp_docs/README.md': FileNode.new(mvir, 'docs').node_id()})
            plans = TreeNode.new(mvir, files={
                'SAFETY_PLAN.md': FileNode.new(mvir, 'plan').node_id(),
                'crisp_docs/README.md': FileNode.new(mvir, 'docs').node_id()})
            session = FileNode.new(mvir, '').node_id()

            def record(outputs, exit_code):
                op = CodexAgentOpNode.new(mvir, inputs={}, outputs=outputs,
                    cmds=[], exit_code=exit_code, raw_output_files=code.node_id(),
                    json_session=session, body='')
                mvir.set_tag('op_history', op.node_id(), op.kind)

            # A successful op whose planning files hold only docs (e.g. a
            # planner that wrote nothing) has no plan to resume.
            record({'code': code.node_id(), 'plans': docs_only.node_id()}, 0)
            self.assertIsNone(prior_agent_plans(mvir, code))
            # A failed op is ignored even if it produced a plan.
            record({'code': code.node_id(), 'plans': plans.node_id()}, 1)
            self.assertIsNone(prior_agent_plans(mvir, code))
            record({'code': code.node_id(), 'plans': plans.node_id()}, 0)
            self.assertEqual(prior_agent_plans(mvir, code).node_id(), plans.node_id())


class CodexLoginFallbackCheckTest(unittest.TestCase):
    def cfg(self, **kwargs):
        return SimpleNamespace(**{'codex_login': True, 'codex_login_fallback': True, **kwargs})

    def test_disabled_fallback_needs_nothing(self):
        with patch.dict(os.environ, {}, clear = True):
            check_codex_login_fallback(self.cfg(codex_login_fallback = False))

    def test_requires_codex_login_and_api_key(self):
        with patch.dict(os.environ, {'CRISP_API_KEY': 'sk-test'}, clear = True):
            with self.assertRaisesRegex(SystemExit, 'requires --codex-login'):
                check_codex_login_fallback(self.cfg(codex_login = False))
        with patch.dict(os.environ, {}, clear = True):
            with self.assertRaisesRegex(SystemExit, 'requires CRISP_API_KEY'):
                check_codex_login_fallback(self.cfg())
        with patch.dict(os.environ, {'CRISP_API_KEY': ''}, clear = True):
            with self.assertRaisesRegex(SystemExit, 'requires CRISP_API_KEY'):
                check_codex_login_fallback(self.cfg())
        with patch.dict(os.environ, {'CRISP_API_KEY': 'sk-test',
                'CRISP_API_BASE': 'https://api.openai.com/v1'}, clear = True):
            check_codex_login_fallback(self.cfg())

    def test_empty_api_base_is_rejected(self):
        # `llm.API_BASE` is read at import time, so set it directly.
        with patch.dict(os.environ, {'CRISP_API_KEY': 'sk-test',
                'CRISP_API_BASE': ''}, clear = True), \
                patch.object(llm, 'API_BASE', ''):
            with self.assertRaisesRegex(SystemExit, 'CRISP_API_BASE'):
                check_codex_login_fallback(self.cfg())


if __name__ == '__main__':
    unittest.main()
