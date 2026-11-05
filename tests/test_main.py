import contextlib
import io
import os
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from crisp import llm
from crisp.__main__ import (
    FuelLimits, check_codex_login_fallback, parse_args, prior_agent_plans,
    safety_loop_common)
from crisp.error import AgentQuotaError
from crisp.mvir import CodexAgentOpNode, FileNode, MVIR, TreeNode
from crisp.workflow import FuelCounter


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

            def record(inputs, outputs):
                op = CodexAgentOpNode.new(mvir, inputs=inputs, outputs=outputs,
                    cmds=[], exit_code=0, raw_output_files=code.node_id(),
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


class SafetyLoopQuotaTest(unittest.TestCase):
    def test_quota_error_stops_the_loop(self):
        steps = []
        def step(*args, **kwargs):
            w.fuel.use()
            steps.append(args)
            raise AgentQuotaError('spend cap')
        w = SimpleNamespace(
            fuel = FuelCounter('safety tries'),
            count_unsafe2 = lambda n_code: 1000,
            do_safety_step_agent = step,
            test_op = lambda n_code, n_c_code: SimpleNamespace(exit_code = 0),
        )
        code = SimpleNamespace(node_id = lambda: 'code')
        limits = FuelLimits(safety_tries = 5, max_consecutive_failures = 5,
            safety_tries_per_target = 5, safety_tries_per_file = 5)
        # The defaults of a real `crisp safety-loop` run.
        with patch('sys.argv', ['crisp', 'safety-loop', '--llm-mode', 'agent']):
            args = parse_args()
        out = io.StringIO()
        with patch('crisp.__main__.get_fuel_limits', return_value = limits), \
                patch('crisp.__main__.prior_agent_plans', return_value = 'plans'), \
                contextlib.redirect_stdout(out):
            safety_loop_common(args, None, None, w, code, code)
        # Other errors are retried, but this one would just fail again.
        self.assertEqual(len(steps), 1)
        self.assertIn('status: API_QUOTA_EXHAUSTED', out.getvalue())


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
