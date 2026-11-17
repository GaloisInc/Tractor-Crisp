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

    def test_legacy_snapshot_resumes_with_its_current_code_as_reference(self):
        raw = cbor.dumps(to_cbor(dict(kind='branch_state',
            c_code=self.c_code.node_id(), code=self.current.node_id(),
            plans=[], failed_plans=[])))
        node_id = NodeId(hashlib.sha256(raw).digest())
        path = Path(self.mvir._node_path(node_id))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

        br = BranchState(self.w, self.mvir.node(node_id), self.limits)
        self.assertEqual(br.orig_code.node_id(), self.current.node_id())
        self.w.agent_safety.return_value = self.candidate, None
        br.agent_safety(self.target)
        self.w.compare_observation_tests_op.assert_called_once_with(
            br.orig_code, self.candidate, br.c_code)
        br.accept()
        mvir = MVIR(self.storage, '.')
        snapshot = mvir.node(mvir.tag('current_branch'))
        self.assertEqual(snapshot.kind, BranchStateNode.KIND)
        self.assertEqual(snapshot.orig_code, self.current.node_id())
        self.assertEqual(snapshot.code, self.candidate.node_id())
