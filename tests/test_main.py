import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from crisp.__main__ import (
    SafetyModelPolicy,
    prior_agent_plans,
    prior_review_findings,
    update_review_feedback,
    update_target_deferrals,
    safety_loop_common, FuelLimits,
)
from crisp.mvir import CodexAgentOpNode, CodexReviewOpNode, FileNode, MVIR, TreeNode
from crisp.config import ModelsConfig
from crisp.workflow import CrispError, FuelCounter, StepOutcome, SAFETY_MODELS


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
                    return StepOutcome(code, plans, None, declared, 'blocked')

                w.do_safety_step_agent.side_effect = refuse
                output = StringIO()
                limits = FuelLimits(3, 2, 10)
                with patch('crisp.__main__.get_fuel_limits', return_value=limits), \
                        patch('crisp.__main__.prior_review_findings', return_value=[]), \
                        patch('crisp.__main__.prior_agent_plans', return_value=plans), \
                        redirect_stdout(output):
                    safety_loop_common(SimpleNamespace(llm_mode='agent'),
                        SimpleNamespace(models=ModelsConfig()), object(), w, code, code)

                # Even an initially valid declaration becomes invalid when
                # that target has already been deferred by the first attempt.
                self.assertEqual(deferred, [
                    frozenset(), frozenset({'crate::real'})])
                self.assertEqual(w.fuel.fuel, 1)
                self.assertIn('status: SATURATED', output.getvalue())
                self.assertIn("attributing attempt to 'crate::second'", output.getvalue())


class SafetyModelPolicyTest(unittest.TestCase):
    def test_escalation_traverses_every_tier_and_stops_at_the_top(self):
        policy = SafetyModelPolicy()
        for level in range(len(SAFETY_MODELS)):
            self.assertEqual(policy.model, SAFETY_MODELS[level])
            policy.update(0)
            self.assertEqual(policy.model, SAFETY_MODELS[level])
            policy.update(0)
        self.assertEqual(policy.model, SAFETY_MODELS[-1])

    def test_rejection_escalates_immediately_and_progress_resets_stalls(self):
        policy = SafetyModelPolicy()
        policy.update(0)
        policy.update(3)
        policy.update(0)
        self.assertEqual(policy.model, SAFETY_MODELS[0])
        policy.update(0, rejected=True)
        self.assertEqual(policy.model, SAFETY_MODELS[1])
        policy.update(0, rejected=True)
        self.assertEqual(policy.model, SAFETY_MODELS[2])

    def test_repeated_returns_double_stays_up_to_eight_steps(self):
        policy = SafetyModelPolicy()
        for stay in (2, 4, 8, 8):
            policy.update(0, rejected=True)
            for _ in range(stay):
                self.assertEqual(policy.model, SAFETY_MODELS[1])
                policy.update(3)
            self.assertEqual(policy.model, SAFETY_MODELS[0])

    def test_slow_progress_requires_a_full_rolling_window(self):
        for reductions in ((1, 2, 1, 4), (1, 1, 1, 6, 1, 1, 1, 1)):
            with self.subTest(reductions=reductions):
                policy = SafetyModelPolicy()
                for removed in reductions[:-1]:
                    policy.update(removed)
                    self.assertEqual(policy.model, SAFETY_MODELS[0])
                policy.update(reductions[-1])
                self.assertEqual(policy.model, SAFETY_MODELS[1])

    def test_progress_window_resets_on_escalation_and_return(self):
        policy = SafetyModelPolicy()
        for _ in range(3):
            policy.update(1)
        policy.update(0, rejected=True)
        policy.update(1)
        self.assertEqual(policy.model, SAFETY_MODELS[1])
        policy.update(3)
        self.assertEqual(policy.model, SAFETY_MODELS[0])
        for _ in range(3):
            policy.update(1)
            self.assertEqual(policy.model, SAFETY_MODELS[0])
        policy.update(1)
        self.assertEqual(policy.model, SAFETY_MODELS[1])

    def test_failed_step_cannot_downshift_when_stay_expires(self):
        policy = SafetyModelPolicy(SAFETY_MODELS[:2])
        policy.update(0, rejected=True)
        for _ in range(10):
            policy.update(0)
            self.assertEqual(policy.model, SAFETY_MODELS[1])
        policy.update(1)
        self.assertEqual(policy.model, SAFETY_MODELS[0])

    def test_single_model_stays_pinned(self):
        policy = SafetyModelPolicy((('custom', 'high'),))
        for _ in range(10):
            policy.update(0, rejected=True)
            policy.update(1)
            self.assertEqual(policy.model, ('custom', 'high'))


class SafetyLoopTest(unittest.TestCase):
    def run_attempts(self, results, targets=None, models=ModelsConfig()):
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
                patch('crisp.__main__.prior_review_findings', return_value=[]), \
                patch('crisp.__main__.prior_agent_plans', return_value=plans), \
                patch('crisp.__main__.traceback.print_exc'), \
                redirect_stdout(StringIO()):
            safety_loop_common(SimpleNamespace(llm_mode='agent'),
                SimpleNamespace(models=models),
                object(), w, code, code)
        self.assertEqual(len(attempts), len(results))
        self.assertEqual(w.fuel.fuel, 0)
        return attempts

    def test_selected_models_follow_accepted_results_and_failures(self):
        for results, levels in [
            (['rejected', 1], [0, 1]),
            (['incomplete', 1], [0, 0]),
            (['error', 'error', 1], [0, 0, 1]),
            (['blocked', 0, 1], [0, 0, 1]),
            (['blocked', 1, 'blocked', 'blocked', 1], [0, 0, 0, 0, 1]),
            ([1, 2, 1, 4, 1], [0, 0, 0, 0, 1]),
            ([1, 2, 1, 5, 1], [0, 0, 0, 0, 0]),
        ]:
            with self.subTest(results=results):
                calls = self.run_attempts(results)
                self.assertEqual([c['worker'] for c in calls],
                    [SAFETY_MODELS[i] for i in levels])

    def test_explicit_model_overrides_disable_the_ladder(self):
        with patch('crisp.__main__.llm.API_MODEL', None):
            calls = self.run_attempts(['rejected', 'rejected', 1],
                models=ModelsConfig(agent_loop='custom'))
        self.assertEqual([c['worker'] for c in calls], [('custom', 'high')] * 3)
        with patch('crisp.__main__.llm.API_MODEL', 'environment-model'):
            calls = self.run_attempts(['rejected', 1])
        self.assertEqual([c['worker'] for c in calls],
            [('environment-model', 'high')] * 2)

    def test_neutral_changes_defer_and_reductions_reopen_targets(self):
        calls = self.run_attempts(['blocked', 0, 1, 'blocked'])
        self.assertEqual(calls[2]['suppressed'], frozenset({'crate::0', 'crate::1'}))
        self.assertEqual(calls[3]['suppressed'], frozenset())

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
