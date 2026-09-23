import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from crisp.__main__ import (
    prior_agent_plans,
    update_review_feedback,
    safety_loop_common, FuelLimits,
)
from crisp.mvir import CodexAgentOpNode, FileNode, MVIR, TreeNode
from crisp.config import ModelsConfig
from crisp.workflow import CrispError, FuelCounter, StepOutcome


class TargetLabelsTest(unittest.TestCase):
    def test_declared_target_labels_reach_the_ledger(self):
        for declared in (None, 'crate::real'):
            with self.subTest(declared=declared):
                code = Mock()
                code.node_id.return_value = 'baseline'
                plans = object()
                w = Mock()
                w.fuel = FuelCounter('test')
                w.count_unsafe2.return_value = 2
                w.test_op.return_value.exit_code = 0

                def refuse(*args, **kwargs):
                    w.fuel.use()
                    return StepOutcome(code, plans, None, declared, 'blocked')

                w.do_safety_step_agent.side_effect = refuse
                output = StringIO()
                limits = FuelLimits(3, 2, 10)
                with patch('crisp.__main__.get_fuel_limits', return_value=limits), \
                        patch('crisp.__main__.prior_agent_plans', return_value=plans), \
                        redirect_stdout(output):
                    safety_loop_common(SimpleNamespace(llm_mode='agent'),
                        SimpleNamespace(models=ModelsConfig()), object(), w, code, code)

                self.assertEqual(w.fuel.fuel, 0)
                self.assertIn('status: BUDGET_EXHAUSTED', output.getvalue())
                self.assertIn(f"refused  {declared or '<unspecified>'}", output.getvalue())


class SafetyLoopTest(unittest.TestCase):
    def run_attempts(self, results, targets=None):
        w = Mock()
        w.fuel = FuelCounter('test')
        code = Mock(unsafe_count=20)
        code.node_id.return_value = 'baseline'
        plans = object()
        w.count_unsafe2.side_effect = lambda tree: tree.unsafe_count
        w.test_op.return_value.exit_code = 0
        attempts = []

        def step(current, c_code, current_plans, **kwargs):
            w.fuel.use()
            index = len(attempts)
            attempts.append(kwargs)
            self.assertIs(current_plans, plans)
            result = results[index]
            if result == 'error':
                raise CrispError('worker failed')
            candidate = current
            report = None
            if result == 'rejected':
                candidate = None
                report = 'CRISP_REVIEW: FAIL\n- [P1] Invalid borrow — src/lib.rs:10'
            elif result == 'incomplete':
                candidate = None
                report = 'CRISP_REVIEW: INCOMPLETE\nNo report.'
            elif isinstance(result, int):
                candidate = Mock(unsafe_count=current.unsafe_count - result)
                candidate.node_id.return_value = f'candidate-{index}'
            return StepOutcome(candidate, plans, report,
                f'crate::{targets[index] if targets else index}', 'blocked')

        w.do_safety_step_agent.side_effect = step
        with patch('crisp.__main__.get_fuel_limits',
                return_value=FuelLimits(len(results), 2, 10)), \
                patch('crisp.__main__.prior_agent_plans', return_value=plans), \
                patch('crisp.__main__.traceback.print_exc'), \
                redirect_stdout(StringIO()):
            safety_loop_common(SimpleNamespace(llm_mode='agent'),
                SimpleNamespace(models=ModelsConfig()),
                object(), w, code, code)
        self.assertEqual(len(attempts), len(results))
        self.assertEqual(w.fuel.fuel, 0)
        return attempts

    def test_reports_survive_other_rejections_reductions_and_target_refusal(self):
        calls = self.run_attempts([
            'rejected', 'rejected', 'blocked', 'blocked', 1,
            'blocked', 1, 0, 'blocked'], targets=[0, 1, 2, 3, 4, 0, 5, 0, 1])
        for i in (2, 5, 6, 7):
            self.assertEqual(set(calls[i]['review_feedback']), {'crate::0', 'crate::1'})
        self.assertEqual(set(calls[8]['review_feedback']), {'crate::1'})


class ReviewFeedbackTest(unittest.TestCase):
    def test_other_targets_keep_their_reports_after_rejection_or_acceptance(self):
        feedback = {'inflate_fast': 'first report'}
        feedback = update_review_feedback(feedback, 'inflate_table',
            report='second report', completed=False)
        self.assertEqual(feedback, {
            'inflate_fast': 'first report', 'inflate_table': 'second report'})
        self.assertEqual(update_review_feedback(feedback, 'unrelated',
            report=None, completed=True), feedback)
        self.assertEqual(update_review_feedback(feedback, 'inflate_table',
            report=None, completed=True), {'inflate_fast': 'first report'})

    def test_latest_report_replaces_only_its_target(self):
        feedback = {'inflate_fast': 'old report', 'inflate_table': 'keep this'}
        self.assertEqual(update_review_feedback(feedback, 'inflate_fast',
            report='new report', completed=False),
            {'inflate_fast': 'new report', 'inflate_table': 'keep this'})

    def test_failed_or_refused_attempt_keeps_feedback(self):
        feedback = {'inflate_fast': 'first report'}
        self.assertEqual(update_review_feedback(feedback, 'inflate_fast',
            report=None, completed=False), feedback)


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


if __name__ == '__main__':
    unittest.main()
