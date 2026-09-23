import copy
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from crisp.__main__ import FuelLimits, safety_loop_common
from crisp.config import ModelsConfig
from crisp.error import CrispError
from crisp.mvir import CodexAgentOpNode, FileNode, MVIR, TreeNode
from crisp.planning import load_progress, planning_interval, save_progress
from crisp.workflow import FuelCounter, StepOutcome, Workflow


class PlanningLoopTest(unittest.TestCase):
    def setUp(self):
        storage = self.enterContext(tempfile.TemporaryDirectory())
        self.mvir = MVIR(storage, storage)
        self.code = TreeNode.new(self.mvir, files={
            'src/lib.rs': FileNode.new(self.mvir, 'original source').node_id()})
        self.c_code = self.code
        self.plans = self.plan('original')
        self.record_plan(self.code, self.plans)
        self.counts = {self.code.node_id(): 100}
        self.w = Mock()
        self.w.fuel = FuelCounter('test')
        self.w.count_unsafe2.side_effect = lambda code: self.counts[code.node_id()]
        self.w.test_op.return_value.exit_code = 0
        self.w.accept.side_effect = lambda code, reason: self.mvir.set_tag('current', code, reason)
        self.w.do_safety_replan_agent.side_effect = self.replan
        self.w.do_safety_step_agent.side_effect = self.step
        self.planning_calls = []
        self.attempts = []
        self.results = []
        self.enterContext(patch('crisp.__main__.traceback.print_exc'))

    def plan(self, label):
        return TreeNode.new(self.mvir, files={
            'SAFETY_PLAN.md': FileNode.new(self.mvir,
                '## FFI entry point rules\nimmutable\n\n## Conventions\nkeep contracts\n'
                f'\n## Cluster guide\n{label}\n').node_id()})

    def record_plan(self, code, plans):
        empty = TreeNode.new(self.mvir, files={})
        op = CodexAgentOpNode.new(self.mvir, inputs={'code': code.node_id()},
            outputs={'code': code.node_id(), 'plans': plans.node_id()},
            cmds=[], exit_code=0, raw_output_files=empty.node_id(),
            json_session=FileNode.new(self.mvir, '').node_id(), body='')
        self.mvir.set_tag('op_history', op, op.kind)

    def replan(self, code, c_code, *, n_plans, progress):
        self.assertEqual(n_plans.node_id(), self.plans.node_id())
        self.planning_calls.append((len(self.attempts), copy.deepcopy(progress)))
        self.plans = self.plan(f'revision {len(self.planning_calls)}')
        self.record_plan(code, self.plans)
        return code, self.plans, 'revised'

    def step(self, code, c_code, plans, **kwargs):
        self.w.fuel.use()
        self.assertEqual(plans.node_id(), self.plans.node_id())
        index = len(self.attempts)
        self.attempts.append(kwargs)
        result = self.results[index] if index < len(self.results) else 'blocked'
        if result == 'error':
            raise CrispError('worker failed')
        if result == 'rejected':
            return StepOutcome(None, None, 'CRISP_REVIEW: FAIL\ninvalid borrow',
                'crate::target', 'review rejected')
        if result == 'plan_exhausted':
            return StepOutcome(code, plans, None, 'crate::target',
                'all ready transformations are complete', plan_exhausted=True)
        if isinstance(result, int):
            candidate = TreeNode.new(self.mvir, files={
                'src/lib.rs': FileNode.new(self.mvir, f'candidate {index}').node_id()})
            self.counts[candidate.node_id()] = self.counts[code.node_id()] - result
            return StepOutcome(candidate, plans, None, 'crate::target', '')
        return StepOutcome(code, plans, None, 'crate::target', 'unresolved ownership')

    def run_loop(self, fuel):
        with patch('crisp.__main__.get_fuel_limits', return_value=FuelLimits(fuel, 2, 10)), \
                redirect_stdout(StringIO()) as output:
            safety_loop_common(SimpleNamespace(llm_mode='agent'),
                SimpleNamespace(models=ModelsConfig()), self.mvir, self.w,
                self.code, self.c_code)
        if self.mvir.has_tag('current'):
            self.code = self.mvir.node(self.mvir.tag('current'))
        self.assertEqual(self.w.fuel.fuel, 0)
        return output.getvalue()

    def test_initial_plan_opens_the_first_window(self):
        interval = planning_interval(self.counts[self.code.node_id()])
        self.w.do_safety_plan_agent.return_value = (self.code, self.plans, '')
        with patch('crisp.__main__.prior_agent_plans', return_value=None):
            self.run_loop(interval + 1)
        self.w.do_safety_plan_agent.assert_called_once()
        self.assertEqual([i for i, _ in self.planning_calls], [interval])

    def test_restart_retains_interval_and_progress_since_last_plan(self):
        self.counts[self.code.node_id()] = 8
        self.run_loop(1)
        # Re-open storage as a new process would; no live Python state needed.
        self.mvir = MVIR(self.mvir._path, self.mvir._src_dir)
        self.run_loop(3)
        self.assertEqual([i for i, _ in self.planning_calls], [0, 3])
        self.assertEqual(len(self.planning_calls[1][1]['steps']), 3)
        self.assertEqual(self.planning_calls[1][1]['baseline_unsafe_count'], 8)

    def test_interval_shrinks_with_the_current_count(self):
        self.counts[self.code.node_id()] = 1024
        self.results = [1016]
        self.run_loop(4)
        self.assertEqual([i for i, _ in self.planning_calls], [0, 3])
        evidence = self.planning_calls[1][1]
        self.assertEqual(evidence['current_unsafe_count'], 8)
        self.assertEqual(evidence['steps'][0]['accepted_reduction'], 1016)

    def test_exhausted_plan_replans_before_next_worker(self):
        self.results = ['plan_exhausted', 1]
        self.run_loop(2)
        self.assertEqual([i for i, _ in self.planning_calls], [0, 1])
        evidence = self.planning_calls[1][1]
        self.assertEqual(evidence['steps'][0]['outcome'], 'plan_exhausted')
        self.assertEqual(evidence['steps'][0]['accepted_reduction'], 0)
        self.assertEqual(evidence['steps'][0]['note'],
            'all ready transformations are complete')
        progress = load_progress(self.mvir, self.code, self.plans)
        self.assertEqual(len(progress['steps']), 1)
        self.assertEqual(progress['steps'][0]['outcome'], 'reduced')

    def test_blocked_and_neutral_steps_do_not_replan_early(self):
        self.results = ['blocked', 0, 1]
        self.run_loop(3)
        self.assertEqual([i for i, _ in self.planning_calls], [0])

    def test_plan_exhaustion_survives_restart_and_waits_for_fuel(self):
        self.results = ['plan_exhausted', 'blocked', 1]
        self.run_loop(1)
        self.run_loop(0)
        self.assertEqual([i for i, _ in self.planning_calls], [0])
        self.mvir = MVIR(self.mvir._path, self.mvir._src_dir)
        self.run_loop(2)
        self.assertEqual([i for i, _ in self.planning_calls], [0, 1])

    def test_no_planning_when_fuel_is_empty_or_count_is_zero(self):
        for count, fuel in ((100, 0), (0, 0)):
            self.counts[self.code.node_id()] = count
            self.run_loop(fuel)
        self.assertEqual(self.planning_calls, [])
        self.w.do_safety_step_agent.assert_not_called()

    def test_zero_ends_without_an_extra_replan(self):
        self.counts[self.code.node_id()] = 1
        self.results = [1]
        self.assertIn('COMPLETE_SAFE', self.run_loop(1))
        self.assertEqual(len(self.planning_calls), 1)

    def test_due_plan_waits_until_next_run_has_worker_fuel(self):
        self.counts[self.code.node_id()] = 8
        self.run_loop(3)
        self.run_loop(0)
        self.assertEqual(len(self.planning_calls), 1)
        self.run_loop(1)
        self.assertEqual([i for i, _ in self.planning_calls], [0, 3])

    def test_progress_is_scoped_to_code_and_plan(self):
        save_progress(self.mvir, self.code, self.plans, {'steps': []})
        self.assertIsNotNone(load_progress(self.mvir, self.code, self.plans))
        self.assertIsNone(load_progress(self.mvir, self.code, self.plan('external revision')))
        other = TreeNode.new(self.mvir, files={})
        self.assertIsNone(load_progress(self.mvir, other, self.plans))

    def test_failed_replan_preserves_previous_window_and_plan(self):
        interval = planning_interval(self.counts[self.code.node_id()])
        self.run_loop(interval)
        previous = load_progress(self.mvir, self.code, self.plans)
        self.w.do_safety_replan_agent.side_effect = CrispError('planner failed')
        with self.assertRaisesRegex(CrispError, 'planner failed'):
            self.run_loop(1)
        self.assertEqual(load_progress(self.mvir, self.code, self.plans), previous)
        self.assertEqual(len(self.attempts), interval)

    def test_interval_boundaries(self):
        for count, expected in ((0, 1), (1, 1), (2, 1), (3, 2), (4, 2),
                (5, 3), (16, 4), (17, 5), (1024, 10), (1025, 11)):
            self.assertEqual(planning_interval(count), expected)


class PlanningWorkflowTest(unittest.TestCase):
    def test_single_invocation_receives_source_inventory_existing_plan_and_progress(self):
        storage = self.enterContext(tempfile.TemporaryDirectory())
        mvir = MVIR(storage, storage)
        cfg = SimpleNamespace(transpile=SimpleNamespace(output_dir='crate'),
            relative_path=lambda p: p, models=ModelsConfig())
        w = Workflow(cfg, mvir)
        code = TreeNode.new(mvir, files={})
        inventory = TreeNode.new(mvir, files={'inventory.json': FileNode.new(mvir, '{}').node_id()})
        w.find_unsafe2_json = Mock(return_value=inventory)
        plans = TreeNode.new(mvir, files={
            'SAFETY_PLAN.md': FileNode.new(mvir, 'current plan').node_id()})
        progress = {'steps': [{'target': 'crate::thing', 'accepted_reduction': 3}]}
        with patch('crisp.workflow.agent.run_rewrite', return_value=(code, code, '')) as run:
            Workflow.do_safety_replan_agent.__wrapped__(w, code, code, plans, progress)
        run.assert_called_once()
        kwargs = run.call_args.kwargs
        self.assertTrue(kwargs['planning_only'])
        self.assertIs(kwargs['unsafe_json'], inventory)
        self.assertIs(kwargs['extra_code']['c_code'], code)
        self.assertEqual(mvir.node(kwargs['extra_code']['progress'].files[
            'SAFETY_PROGRESS.json']).body_json(), progress)
        self.assertIs(kwargs['planning_files'], plans)
        self.assertIn('NET reduction', run.call_args.args[2])
        self.assertEqual(w.fuel.fuel, 0)
