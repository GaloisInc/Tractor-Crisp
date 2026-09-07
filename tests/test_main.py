import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from crisp.__main__ import (
    prior_review_findings,
    update_review_feedback,
    update_target_deferrals,
    safety_loop_common, FuelLimits,
)
from crisp.mvir import CodexReviewOpNode, FileNode, MVIR, TreeNode
from crisp.config import ModelsConfig
from crisp.workflow import CrispError, FuelCounter, StepOutcome


class TargetDeferralsTest(unittest.TestCase):
    def test_failures_expire_after_an_accepted_reduction(self):
        deferred = set()
        update_target_deferrals(deferred, 'inflate_fast', reduced=False)
        update_target_deferrals(deferred, 'inflate_table', reduced=False)
        self.assertEqual(deferred, {'inflate_fast', 'inflate_table'})

        update_target_deferrals(deferred, 'other_target', reduced=True)
        self.assertEqual(deferred, set())

    def test_invalid_declarations_cannot_bypass_saturation(self):
        for declared in (None, 'real', 'stale::target', 'crate::real'):
            with self.subTest(declared=declared):
                code = Mock()
                code.node_id.return_value = 'baseline'
                plans = object()
                w = Mock()
                w.fuel = FuelCounter('test')
                w.count_unsafe2.return_value = 2
                w.fn_records.return_value = {
                    'crate::real': {'total_unsafe': 1},
                    'crate::second': {'total_unsafe': 1},
                }
                w.type_records.return_value = {}
                w.test_op.return_value.exit_code = 0
                deferred = []

                def refuse(*args, **kwargs):
                    w.fuel.use()
                    deferred.append(kwargs['suppressed'])
                    return StepOutcome(code, plans, None, declared, 'blocked', 1)

                w.do_safety_step_agent.side_effect = refuse
                output = StringIO()
                limits = FuelLimits(3, 2, 10, 1)
                with patch('crisp.__main__.get_fuel_limits', return_value=limits), \
                        patch('crisp.__main__.prior_review_findings', return_value=[]), \
                        patch('crisp.__main__.prior_agent_plans', return_value=plans), \
                        redirect_stdout(output):
                    safety_loop_common(SimpleNamespace(llm_mode='agent'),
                        SimpleNamespace(models=ModelsConfig()), object(), w, code, code)

                # Even an initially valid declaration becomes invalid when
                # that target has already been deferred by the first attempt.
                self.assertEqual(deferred, [
                    frozenset(), frozenset({'crate::real'}), frozenset()])
                self.assertEqual(w.fuel.fuel, 0)
                self.assertEqual([c.kwargs['model'] for c in
                    w.do_safety_step_agent.call_args_list],
                    ['gpt-5.6-terra', 'gpt-5.6-terra', 'gpt-6-astra'])
                self.assertIn('status: SATURATED', output.getvalue())
                self.assertIn("attributing attempt to 'crate::second'", output.getvalue())


class SafetyRescueTest(unittest.TestCase):
    def run_attempts(self, results, targets=None):
        w = Mock()
        w.fuel = FuelCounter('test')
        code = Mock(unsafe_count=20)
        code.node_id.return_value = 'baseline'
        plans = object()
        w.count_unsafe2.side_effect = lambda tree: tree.unsafe_count
        w.fn_records.return_value = {
            f'crate::{i}': {'total_unsafe': 1} for i in range(10)}
        w.type_records.return_value = {}
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
                report = 'Review rejected the change'
            elif isinstance(result, int):
                candidate = Mock(unsafe_count=current.unsafe_count - result)
                candidate.node_id.return_value = f'candidate-{index}'
            return StepOutcome(candidate, plans, report,
                f'crate::{targets[index] if targets else index}', 'blocked', 1)

        w.do_safety_step_agent.side_effect = step
        with patch('crisp.__main__.get_fuel_limits',
                return_value=FuelLimits(len(results), 2, 10, 1)), \
                patch('crisp.__main__.prior_review_findings', return_value=[]), \
                patch('crisp.__main__.prior_agent_plans', return_value=plans), \
                patch('crisp.__main__.traceback.print_exc'), \
                redirect_stdout(StringIO()):
            safety_loop_common(SimpleNamespace(llm_mode='agent'),
                SimpleNamespace(models=ModelsConfig(
                    agent_loop='loop-model', agent_rescue='rescue-model')),
                object(), w, code, code)
        self.assertEqual(len(attempts), len(results))
        self.assertEqual(w.fuel.fuel, 0)
        self.assertTrue(all(c['max_invocations'] == 1 for c in attempts))
        return attempts

    def test_failed_rescue_returns_to_loop_and_preserves_deferrals(self):
        for rescue_result in ('blocked', 'rejected', 'error'):
            with self.subTest(rescue_result=rescue_result):
                calls = self.run_attempts([
                    'blocked', 'rejected', rescue_result, 'blocked', 'blocked', 'blocked'])
                self.assertEqual([c['model'] for c in calls],
                    ['loop-model', 'loop-model', 'rescue-model'] * 2)
                self.assertEqual(calls[2]['suppressed'], frozenset())
                self.assertTrue({'crate::0', 'crate::1'} <= calls[3]['suppressed'])
                self.assertEqual(calls[2]['review_feedback']['crate::1'],
                    'Review rejected the change')

    def test_accepted_neutral_rescue_reopens_targets(self):
        calls = self.run_attempts(['blocked', 0, 0, 'blocked'])
        self.assertEqual([c['model'] for c in calls],
            ['loop-model', 'loop-model', 'rescue-model', 'loop-model'])
        self.assertEqual(calls[3]['suppressed'], frozenset())

    def test_single_operation_reduction_resets_streak(self):
        calls = self.run_attempts(['blocked', 1, 'blocked', 'blocked', 1, 'blocked'])
        self.assertEqual([c['model'] for c in calls],
            ['loop-model'] * 4 + ['rescue-model', 'loop-model'])
        self.assertEqual(calls[2]['suppressed'], frozenset())
        self.assertEqual(calls[5]['suppressed'], frozenset())

    def test_worker_errors_count_toward_rescue(self):
        calls = self.run_attempts(['error', 'error', 'blocked'])
        self.assertEqual([c['model'] for c in calls],
            ['loop-model', 'loop-model', 'rescue-model'])

    def test_neutral_rescue_reopens_only_once_until_a_reduction(self):
        calls = self.run_attempts([
            'blocked', 'blocked', 0, 'blocked', 'blocked', 0,
            1, 'blocked', 'blocked', 0, 'blocked'])
        self.assertEqual(calls[3]['suppressed'], frozenset())
        self.assertEqual(calls[6]['suppressed'],
            frozenset({'crate::3', 'crate::4', 'crate::5'}))
        self.assertEqual(calls[10]['suppressed'], frozenset())


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


class PersistentSafetyMemoryTest(unittest.TestCase):
    def test_failed_review_findings_survive_restart_recovery(self):
        with tempfile.TemporaryDirectory() as storage:
            mvir = MVIR(storage, '.')
            code = TreeNode.new(mvir, files={})
            prompt = FileNode.new(mvir, 'review prompt')
            report = FileNode.new(mvir,
                '- [P1] Keep wrapper thin — src/gzlib.rs:10-20')
            review = CodexReviewOpNode.new(
                mvir,
                old_code=code.node_id(),
                new_code=code.node_id(),
                raw_prompt=prompt.node_id(),
                report=report.node_id(),
                verdict='FAIL',
                body='review logs',
            )
            mvir.set_tag('op_history', review.node_id(), review.kind)

            self.assertEqual(prior_review_findings(mvir),
                ['Keep wrapper thin'])


if __name__ == '__main__':
    unittest.main()
