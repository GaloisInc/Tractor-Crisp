import contextlib
import io
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from crisp.__main__ import (
    FuelLimits, parse_args, prior_agent_plans, safety_loop_common)
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


if __name__ == '__main__':
    unittest.main()
