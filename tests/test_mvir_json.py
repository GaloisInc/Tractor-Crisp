import contextlib
from datetime import datetime
import io
import json
import math
from pathlib import Path
import tempfile
import unittest

from crisp.mvir import (
    BranchStateNode, CompileCommandsOpNode, FileNode, MVIR, TreeNode,
    WorkflowStepNode,
)
from crisp.mvir_json import export_node, from_json, import_node, to_json
from crisp.node_id import NodeId
from crisp.planning import StepGoal, StepPlan


class MetadataJsonTest(unittest.TestCase):
    def test_scalar_tags_and_string_escaping(self):
        node_id = NodeId(bytes(range(NodeId.LENGTH)))
        cases = [
            (None, None), (False, False), (True, True),
            (42, 42), (3.5, 3.5), ('', ''), ('plain', 'plain'),
            ('foo!bar', '!foo!bar'), ('!foo', '!!foo'), ('!', '!!'),
            ('n!' + str(node_id), '!n!' + str(node_id)),
            (node_id, 'n!' + str(node_id)),
            (b'', 'b64!'), (b'\x00\xff\x80', 'b64!AP+A'),
            (datetime(2026, 11, 17, 12, 34, 56, 123456),
                'dt!2026-11-17T12:34:56.123456'),
        ]
        for value, encoded in cases:
            with self.subTest(value=value):
                self.assertEqual(to_json(value), encoded)
                decoded = from_json(json.loads(json.dumps(encoded)))
                self.assertEqual(decoded, value)
                self.assertIs(type(decoded), type(value))

    def test_nested_containers_and_escaped_dictionary_keys(self):
        value = {
            'a!b': [None, (b'\x00', ['!literal', False])],
            '!tuple': 'a literal key',
            '!StepPlan': {},
            'n!abc': {},
            '!': '!',
            '': {('x', 'y'): b'\xff', 42: 'an integer key'},
        }
        encoded = to_json(value)
        self.assertEqual(encoded['!a!b'][1],
            {'!tuple': ['b64!AA==', ['!!literal', False]]})
        decoded = from_json(json.loads(json.dumps(encoded)))
        self.assertEqual(decoded, value)
        self.assertIsInstance(decoded['a!b'][1], tuple)
        self.assertIsInstance(next(iter(decoded[''])), tuple)

    def test_non_finite_floats(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                encoded = json.dumps(to_json(value), allow_nan=False)
                decoded = from_json(json.loads(encoded))
                if math.isnan(value):
                    self.assertTrue(math.isnan(decoded))
                else:
                    self.assertEqual(decoded, value)

    def test_invalid_tags_and_records_are_rejected(self):
        for value in (
            'unknown!value', 'n!1234', 'n!' + 'z' * 64,
            'b64!%%%', 'b64!a', 'dt!not-a-date', 'f!1.5',
            {'!tuple': 'not a list'}, {'!dict': [1]},
            {'!unknown': []}, {'!tuple': [], 'extra': 1},
            {'!StepGoal': {'key': 7}},
            {'!StepGoal': {'key': 'total_unsafe', 'files': 7}},
            {'!StepPlan': {'description': 'x', 'goals': ['not a goal']}},
            {'!StepPlan': {'description': 'x', 'extra': 1}},
            {'a': 1, '!a': 2},
        ):
            with self.subTest(value=value):
                with self.assertRaises((ValueError, TypeError)):
                    from_json(value)


class NodeExportImportTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = MVIR(self.root / 'source', '.')
        self.destination = MVIR(self.root / 'destination', '.')
        self.path = self.root / 'node.json'
        self.body_path = self.root / 'node.json.body'

    def round_trip(self, node):
        # Load a fresh object from disk so this also covers CBOR deserialization.
        node = MVIR(self.source._path, '.').node(node.node_id())
        export_node(node, self.path)
        self.assertTrue(self.path.read_text().endswith('\n'))
        self.assertEqual(self.body_path.read_bytes(), node.body())
        imported = import_node(self.destination, self.path)
        self.assertEqual(imported.node_id(), node.node_id())
        self.assertEqual(imported.metadata(), node.metadata())
        self.assertEqual(imported.body(), node.body())
        loaded = MVIR(self.destination._path, '.').node(imported.node_id())
        self.assertEqual(loaded.metadata(), node.metadata())
        return loaded

    def test_file_body_is_binary_and_can_be_empty(self):
        for body in (b'', bytes(range(256)) + b'\x00\r\n'):
            with self.subTest(body=body):
                self.round_trip(FileNode.new(self.source, body))

    def test_tree_preserves_node_references_and_unusual_filenames(self):
        file = FileNode.new(self.source, b'file contents')
        node = TreeNode.new(self.source, files={
            'foo!bar.rs': file.node_id(),
            '!tuple': file.node_id(),
            'src/\u03bb.rs': file.node_id(),
        })
        self.round_trip(node)
        self.assertIn('src/\u03bb.rs', self.path.read_text(encoding='utf-8'))
        # Export/import is not a recursive archive of referenced nodes.
        self.assertFalse(Path(self.destination._node_path(file.node_id())).exists())

    def test_optional_reference_commands_and_exit_code(self):
        ref = NodeId(b'\x01' * NodeId.LENGTH)
        for compile_commands in (None, ref):
            with self.subTest(compile_commands=compile_commands):
                node = CompileCommandsOpNode.new(self.source, c_code=ref,
                    cmds=[['sh', '-c', 'echo hello!']], exit_code=1,
                    compile_commands=compile_commands)
                self.round_trip(node)

    def test_timestamp_round_trip(self):
        ref = NodeId(b'\x02' * NodeId.LENGTH)
        node = WorkflowStepNode.new(self.source, inputs=ref, output=ref,
            timestamp=datetime(2026, 11, 17, 12, 34, 56, 123456))
        loaded = self.round_trip(node)
        self.assertIsInstance(loaded.timestamp, datetime)

    def test_nested_planning_records_keep_named_fields_and_types(self):
        ref = NodeId(b'\x03' * NodeId.LENGTH)
        plan = StepPlan('Remove unsafe!', goals=[
            StepGoal(('uses_foreign_fn', 'printf'), max_count=2,
                files=['src/main.rs'], functions='main.*'),
            StepGoal('total_unsafe', types=['Thing']),
        ], details=[ref], subdiv_attempts=1)
        node = BranchStateNode.new(self.source, c_code=ref, code=ref,
            plans=[plan], failed_plans=[StepPlan('Retry later')])
        loaded = self.round_trip(node)
        self.assertIsInstance(loaded.plans[0], StepPlan)
        self.assertIsInstance(loaded.plans[0].goals[0], StepGoal)
        self.assertIsInstance(loaded.plans[0].goals[0].key, tuple)
        metadata = json.loads(self.path.read_text())
        metadata['plans'][0]['!StepPlan']['description'] = 'Edited plan'
        self.path.write_text(json.dumps(metadata))
        edited = import_node(self.destination, self.path)
        self.assertEqual(edited.plans[0].description, 'Edited plan')
        self.assertNotEqual(edited.node_id(), node.node_id())

    def test_manual_metadata_and_body_edits_create_a_new_node(self):
        original = TreeNode.new(self.source, files={})
        export_node(original, self.path)
        ref = NodeId(b'\x04' * NodeId.LENGTH)
        self.path.write_text(json.dumps({
            'kind': 'tree', 'files': {'new.rs': 'n!' + str(ref)}}))
        self.body_path.write_bytes(b'edited body\x00\xff')
        edited = import_node(self.source, self.path)
        self.assertEqual(edited.files, {'new.rs': ref})
        self.assertEqual(edited.body(), b'edited body\x00\xff')
        self.assertNotEqual(edited.node_id(), original.node_id())
        self.assertEqual(original.files, {})
        self.assertEqual(original.body(), b'')

    def test_invalid_metadata_does_not_write_a_node(self):
        self.body_path.write_bytes(b'body')
        for metadata in (
            '{}', '[]', '{"kind":"unknown"}',
            '{"kind":"file","extra":1}', '{"kind":"tree"}',
            '{"kind":"tree","files":{"x":"not a node ID"}}',
            '{"kind":"file","kind":"tree"}',
            '{"kind":"file","extra":NaN}',
            json.dumps({'kind': 'compile_commands_op_v2',
                'c_code': 'n!' + '00' * 32, 'cmds': [], 'exit_code': 0,
                'compile_commands': 'not a node ID'}),
        ):
            with self.subTest(metadata=metadata):
                self.path.write_text(metadata)
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises((ValueError, TypeError, AssertionError)):
                        import_node(self.destination, self.path)
                self.assertFalse(Path(self.destination._path).exists())

    def test_missing_body_is_an_error(self):
        self.path.write_text('{"kind":"file"}')
        with self.assertRaises(FileNotFoundError):
            import_node(self.destination, self.path)
        self.assertFalse(Path(self.destination._path).exists())


if __name__ == '__main__':
    unittest.main()
