import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crisp.__main__ import main, prior_agent_plans
from crisp.mvir import CodexAgentOpNode, FileNode, MVIR, TreeNode
from crisp.node_id import NodeId


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


class ExportImportCommandTest(unittest.TestCase):
    def test_export_and_import_through_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory) / 'storage'
            path = Path(directory) / 'node.json'
            cfg = SimpleNamespace(mvir_storage_dir=storage)
            mvir = MVIR(storage, '.')
            file = FileNode.new(mvir, b'\x00\xfforiginal')
            tree = TreeNode.new(mvir, files={'foo!bar': file.node_id()})
            mvir.set_tag('current', tree.node_id())

            def run(*args):
                output = io.StringIO()
                with patch('sys.argv', ['crisp', *args]), \
                        patch('crisp.__main__.Config.from_toml_file', return_value=cfg), \
                        contextlib.redirect_stdout(output):
                    main()
                return output.getvalue()

            # Export accepts tags, prefixes, and the existing ref expressions.
            for ref in ('current.files["foo!bar"]', str(file.node_id())[:12]):
                run('export', ref, str(path))
                self.assertEqual(json.loads(path.read_text()), {'kind': 'file'})
                self.assertEqual(Path(str(path) + '.body').read_bytes(), file.body())
            self.assertEqual(run('import', str(path)).strip(), str(file.node_id()))
            self.assertEqual(mvir.tag('current'), tree.node_id())

            Path(str(path) + '.body').write_bytes(b'edited\x00\xff')
            new_id = NodeId.from_str(run('import', str(path), '--tag', 'current').strip())
            self.assertNotEqual(new_id, file.node_id())
            self.assertEqual(mvir.tag('current'), new_id)
            self.assertEqual(mvir.node(new_id).body(), b'edited\x00\xff')
            self.assertEqual(mvir.tag_reflog('current')[-1].reason, 'import')


if __name__ == '__main__':
    unittest.main()
