import contextlib
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import cbor

from crisp.branch import BranchState
from crisp.error import CrispError
from crisp.mvir import MVIR, BranchStateNode, FileNode, TreeNode, to_cbor
from crisp.node_id import NodeId
from crisp.planning import StepPlan, StepGoal
from crisp.util import FuelLimits
from crisp.workflow import AgentTargetOther, Workflow


class BranchStateTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.storage = directory.name
        self.mvir = MVIR(self.storage, '.')
        self.c_code = self.tree('api.h', 'int api(void);')
        self.original = self.tree('rust/lib.rs', 'original')
        self.current = self.tree('rust/lib.rs', 'first rewrite')
        self.candidate = self.tree('rust/lib.rs', 'second rewrite')
        self.limits = FuelLimits(safety_tries=5, max_consecutive_failures=3,
                                 safety_tries_per_target=3, safety_tries_per_file=3)
        self.w = self.workflow(self.mvir)
        self.target = AgentTargetOther()
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def tree(self, path, body):
        return TreeNode.new(self.mvir, files={
            path: FileNode.new(self.mvir, body).node_id()})

    def workflow(self, mvir):
        w = Mock(spec=Workflow)
        w.mvir = mvir
        w.test_op.return_value = SimpleNamespace(exit_code=0)
        w.compare_observation_tests_op.return_value = SimpleNamespace(exit_code=0)
        w.check_unsafe2_op.return_value = SimpleNamespace(exit_code=0)
        w.ffi_review_op.return_value = None
        return w

    def test_original_reference_survives_rewrites_and_reload(self):
        br = BranchState.new(self.w, self.limits, self.c_code, self.original)
        self.assertEqual(br.orig_code.node_id(), self.original.node_id())
        self.w.agent_safety.return_value = self.current, None
        br.agent_safety(self.target)
        self.w.compare_observation_tests_op.assert_called_once_with(
            self.original, self.current, self.c_code)
        br.accept()

        # Load from disk in a fresh MVIR, then perform another rewrite.
        mvir = MVIR(self.storage, '.')
        w = self.workflow(mvir)
        br = BranchState(w, mvir.node(mvir.tag('current_branch')), self.limits)
        self.assertEqual(br.orig_code.node_id(), self.original.node_id())
        self.assertEqual(br.code.node_id(), self.current.node_id())
        old_code = br.code
        w.agent_safety.return_value = self.candidate, None
        br.agent_safety(self.target)
        w.agent_safety.assert_called_once_with(old_code, br.c_code, None,
            target_goal=self.target, n_orig_code=br.orig_code)
        w.compare_observation_tests_op.assert_called_once_with(
            br.orig_code, self.candidate, br.c_code)
        # Safety and FFI checks still compare against the previous revision.
        w.find_unsafe2_json.assert_called_once_with(old_code)
        w.ffi_review_op.assert_called_once_with(old_code, self.candidate)
        snapshot = br._snapshot()
        self.assertEqual(snapshot.orig_code, self.original.node_id())
        self.assertEqual(snapshot.code, self.candidate.node_id())

    def test_failed_observations_restore_code_and_keep_original_reference(self):
        snapshot = BranchStateNode.new(self.mvir,
            c_code=self.c_code.node_id(), orig_code=self.original.node_id(),
            code=self.current.node_id(), plans=[], failed_plans=[])
        br = BranchState(self.w, snapshot, self.limits)
        self.w.agent_safety.return_value = self.candidate, None
        self.w.compare_observation_tests_op.return_value = SimpleNamespace(exit_code=1)
        with self.assertRaisesRegex(CrispError, 'failed the diff tests'):
            br.agent_safety(self.target)
        self.w.compare_observation_tests_op.assert_called_once_with(
            self.original, self.candidate, self.c_code)
        self.assertEqual(br._snapshot().node_id(), snapshot.node_id())
        self.assertEqual(br.orig_code.node_id(), self.original.node_id())
        self.assertEqual(br.code.node_id(), self.current.node_id())

    def test_legacy_snapshot_migrates_metadata_but_cannot_resume(self):
        raw = cbor.dumps(to_cbor(dict(kind='branch_state',
            c_code=self.c_code.node_id(), code=self.current.node_id(),
            plans=[], failed_plans=[])))
        node_id = NodeId(hashlib.sha256(raw).digest())
        path = Path(self.mvir._node_path(node_id))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

        snapshot = self.mvir.node(node_id)
        self.assertEqual(snapshot.kind, BranchStateNode.KIND)
        self.assertEqual(snapshot.orig_code, self.current.node_id())
        self.assertEqual(snapshot.code, self.current.node_id())
        self.assertEqual(snapshot.node_id(), node_id)

        # Migration permits reading old metadata, but resuming a branch requires
        # a snapshot written with the current schema and its corresponding hash.
        with self.assertRaises(AssertionError):
            BranchState(self.w, snapshot, self.limits)

    def test_plan_tests_are_reused_after_failed_rewrite_and_regenerated_on_switch(self):
        br = BranchState.new(self.w, self.limits, self.c_code, self.original)
        plan = StepPlan('Make api safe', goals=[StepGoal('total_unsafe', functions=['api'])])
        br.plans = [plan]
        br.update_plans = Mock()
        self.w.find_unsafe2_json_dicts.return_value = []
        tested = TreeNode.new(self.mvir, files={**self.original.files,
            'crisp_tests/tests/api.c': FileNode.new(self.mvir, '// RUN: test_api').node_id()})
        self.w.agent_gen_tests.return_value = tested

        target = br.pick_next_target()
        self.w.agent_gen_tests.assert_called_once_with(
            self.original, self.c_code, plan, n_orig_code=self.original)
        self.w.agent_safety.return_value = self.candidate, None
        self.w.compare_observation_tests_op.return_value = SimpleNamespace(exit_code=1)
        with self.assertRaises(CrispError):
            br.agent_safety(target)
        self.assertEqual(br.code.node_id(), tested.node_id())

        # Rollback reparses plans, so identity must survive different objects.
        target = br.pick_next_target()
        self.w.agent_gen_tests.assert_called_once()
        self.w.compare_observation_tests_op.return_value = SimpleNamespace(exit_code=0)
        br.agent_safety(target)
        self.assertEqual(self.w.agent_safety.call_args.args[0].node_id(), tested.node_id())

        next_plan = StepPlan('Make other safe', goals=[StepGoal('total_unsafe')])
        br.plans = [next_plan]
        self.w.agent_gen_tests.return_value = self.candidate
        br.pick_next_target()
        self.assertEqual(self.w.agent_gen_tests.call_count, 2)
        self.w.agent_gen_tests.assert_called_with(
            self.candidate, self.c_code, next_plan, n_orig_code=br.orig_code)

    def test_failed_generation_is_logged_and_does_not_block_or_repeat_for_plan(self):
        br = BranchState.new(self.w, self.limits, self.c_code, self.original)
        br.plans = [StepPlan('Make api safe')]
        br.update_plans = Mock()
        self.w.find_unsafe2_json_dicts.return_value = []
        self.w.agent_gen_tests.side_effect = CrispError('invalid generated tests')

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            target = br.pick_next_target()
        self.assertIn('test generation failed', output.getvalue())
        self.assertIn('invalid generated tests', output.getvalue())
        self.assertEqual(br.code.node_id(), self.original.node_id())

        self.w.agent_safety.return_value = self.current, None
        br.agent_safety(target)
        self.w.agent_safety.assert_called_once_with(
            self.original, self.c_code, None, target_goal=target, n_orig_code=br.orig_code)
        br.pick_next_target()
        self.w.agent_gen_tests.assert_called_once()

    def test_fallback_target_does_not_generate_tests(self):
        br = BranchState.new(self.w, self.limits, self.c_code, self.original)
        br.update_plans = Mock()
        self.assertIsInstance(br.pick_next_target(), AgentTargetOther)
        self.w.agent_gen_tests.assert_not_called()
