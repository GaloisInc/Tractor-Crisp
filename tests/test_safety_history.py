import json
from pathlib import Path
import runpy
import tempfile
import unittest

from crisp.mvir import (
    CheckUnsafe2AnalysisNode,
    CodexAgentOpNode,
    FileNode,
    FindUnsafe2AnalysisNode,
    MVIR,
    TestResultNode,
    TreeNode,
)
from crisp.safety_history import build_safety_history


class SafetyHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mvir = MVIR(self.tmp.name, ".")
        self.empty_tree = TreeNode.new(self.mvir, files={})
        self.unsafe_trees = {}

    def tearDown(self):
        self.tmp.cleanup()

    def code(self, name):
        source = FileNode.new(self.mvir, f"// {name}\n")
        return TreeNode.new(self.mvir, files={"lib.rs": source.node_id()})

    def add_unsafe_count(self, code, count):
        report = FileNode.new(self.mvir, json.dumps({"total_unsafe": count}))
        reports = TreeNode.new(
            self.mvir, files={"crate.json": report.node_id()}
        )
        FindUnsafe2AnalysisNode.new(
            self.mvir,
            code=code.node_id(),
            cmd=["cargo", "find-unsafe2"],
            exit_code=0,
            unsafe_json=reports.node_id(),
        )
        self.unsafe_trees[str(code.node_id())] = reports

    def session(self, message, increase=False):
        events = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 40,
                        "output_tokens": 10,
                    }},
                },
            },
            {
                "timestamp": "2026-01-01T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({
                        "cmd": "cargo check-unsafe2 --manifest-path rust/Cargo.toml"
                    }),
                    "call_id": "check-call",
                },
            },
            {
                "timestamp": "2026-01-01T00:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "check-call",
                    "output": (
                        "foo: unsafe function calls increased: 0 -> 1"
                        if increase else "check passed"
                    ),
                },
            },
            {
                "timestamp": "2026-01-01T00:00:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": message,
                    "duration_ms": 2500,
                },
            },
        ]
        return FileNode.new(
            self.mvir, "\n".join(json.dumps(event) for event in events)
        )

    def session_file(self, events):
        return FileNode.new(
            self.mvir, "\n".join(json.dumps(event) for event in events)
        )

    def add_agent(self, old_code, new_code, message, accepted, check_body=""):
        op = CodexAgentOpNode.new(
            self.mvir,
            old_code=old_code.node_id(),
            new_code=new_code.node_id(),
            raw_prompt=FileNode.new(self.mvir, "prompt").node_id(),
            exit_code=0,
            raw_output_files=self.empty_tree.node_id(),
            json_session=self.session(message, bool(check_body)).node_id(),
            planning_files=self.empty_tree.node_id(),
            body="agent output",
        )
        self.mvir.set_tag("op_history", op.node_id(), op.kind)

        test = TestResultNode.new(
            self.mvir,
            code=new_code.node_id(),
            test_code=self.empty_tree.node_id(),
            cmd="true",
            exit_code=0,
            body="tests passed",
        )
        self.mvir.set_tag("test_results", test.node_id())

        baseline = self.unsafe_trees[str(old_code.node_id())]
        CheckUnsafe2AnalysisNode.new(
            self.mvir,
            code=new_code.node_id(),
            unsafe_json=baseline.node_id(),
            cmd=["cargo", "check-unsafe2"],
            exit_code=0 if accepted else 1,
            body=check_body,
        )
        if accepted:
            self.mvir.set_tag(
                "current", new_code.node_id(), ["main", "safety", 1]
            )
        return op

    def build_fixture(self):
        initial = self.code("initial")
        accepted = self.code("accepted")
        rejected = self.code("rejected")
        corrected = self.code("corrected")
        self.add_unsafe_count(initial, 10)
        self.add_unsafe_count(accepted, 7)
        self.add_unsafe_count(corrected, 3)

        op1 = self.add_agent(
            initial, accepted, "Removed three unsafe operations.", True
        )
        op2 = self.add_agent(
            accepted,
            rejected,
            "Tried a candidate refactor.",
            False,
            "foo: unsafe function calls increased: 0 -> 1",
        )
        op3 = self.add_agent(
            accepted, corrected, "Corrected the rejected refactor.", True
        )
        return op1, op2, op3

    def test_builds_accepted_and_rejected_history(self):
        op1, op2, op3 = self.build_fixture()
        data = build_safety_history(self.mvir)

        self.assertEqual([row["result"] for row in data["rows"]], [
            "accepted", "rejected", "accepted",
        ])
        self.assertEqual([row["after_count"] for row in data["rows"]], [7, 7, 3])
        self.assertEqual([row["delta"] for row in data["rows"]], [-3, 0, -4])
        self.assertEqual(data["rows"][1]["rejection_reason"],
            "foo: unsafe function calls increased: 0 -> 1")
        self.assertEqual(data["rows"][0]["tokens_used"], 70)
        self.assertEqual(data["rows"][0]["agent_duration_seconds"], 2.5)
        self.assertEqual(
            data["rows"][1]["internal_check_unsafe2_increase_count"], 1
        )
        self.assertEqual(data["aggregate"]["accepted_edits"], 2)
        self.assertEqual(data["aggregate"]["rejected_edits"], 1)
        self.assertEqual(data["aggregate"]["final_unsafe_count"], 3)
        self.assertEqual(data["checkpoint"]["last_agent_op"], str(op3.node_id()))
        self.assertEqual(data["coverage_warnings"], [])

    def test_after_is_exclusive_and_preserves_full_aggregate(self):
        _, op2, op3 = self.build_fixture()
        data = build_safety_history(self.mvir, after=op2.node_id())

        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["agent_op"], str(op3.node_id()))
        self.assertEqual(data["selection"], {
            "after": str(op2.node_id()),
            "agent_op": None,
            "returned_rows": 1,
        })
        self.assertEqual(data["aggregate"]["completed_edits"], 3)

    def test_after_must_be_an_agent_operation_in_history(self):
        self.build_fixture()
        unrelated = FileNode.new(self.mvir, "unrelated")
        with self.assertRaisesRegex(ValueError, "not present in safety history"):
            build_safety_history(self.mvir, after=unrelated.node_id())

    def test_agent_op_selects_one_globally_numbered_row(self):
        _, op2, _ = self.build_fixture()
        data = build_safety_history(self.mvir, agent_op=op2.node_id())

        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["number"], 2)
        self.assertEqual(data["rows"][0]["agent_op"], str(op2.node_id()))
        self.assertEqual(data["selection"], {
            "after": None,
            "agent_op": str(op2.node_id()),
            "returned_rows": 1,
        })
        self.assertEqual(data["aggregate"]["completed_edits"], 3)

    def test_agent_op_and_after_are_mutually_exclusive(self):
        op1, op2, _ = self.build_fixture()
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            build_safety_history(
                self.mvir, after=op1.node_id(), agent_op=op2.node_id()
            )

    def test_internal_output_is_opt_in(self):
        op1, _, _ = self.build_fixture()

        default = build_safety_history(self.mvir, agent_op=op1.node_id())
        self.assertNotIn(
            "output", default["rows"][0]["internal_check_unsafe2_runs"][0]
        )

        detailed = build_safety_history(
            self.mvir,
            agent_op=op1.node_id(),
            include_internal_output=True,
        )
        self.assertEqual(
            detailed["rows"][0]["internal_check_unsafe2_runs"][0]["output"],
            "check passed",
        )

        commands = build_safety_history(
            self.mvir,
            agent_op=op1.node_id(),
            include_agent_commands=True,
        )
        self.assertEqual(
            commands["rows"][0]["agent_commands"][0]["command"],
            "cargo check-unsafe2 --manifest-path rust/Cargo.toml",
        )

    def test_heredoc_mentions_are_not_checker_runs(self):
        old_code = self.code("old")
        new_code = self.code("new")
        self.add_unsafe_count(old_code, 1)
        self.add_unsafe_count(new_code, 0)
        events = [{
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "plan",
                "arguments": json.dumps({
                    "cmd": "cat <<'EOF' > PLAN\ncargo check-unsafe2\nEOF"
                }),
            },
        }]
        op = CodexAgentOpNode.new(
            self.mvir,
            old_code=old_code.node_id(),
            new_code=new_code.node_id(),
            raw_prompt=FileNode.new(self.mvir, "prompt").node_id(),
            exit_code=0,
            raw_output_files=self.empty_tree.node_id(),
            json_session=self.session_file(events).node_id(),
            planning_files=self.empty_tree.node_id(),
            body="agent output",
        )
        self.mvir.set_tag("op_history", op.node_id(), op.kind)

        data = build_safety_history(self.mvir)
        self.assertEqual(data["rows"][0]["internal_check_unsafe2_count"], 0)


class SummaryRendererTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = (
            Path(__file__).parents[1]
            / "skills/summarize-crisp-run/scripts/render_summary.py"
        )
        renderer = runpy.run_path(script)
        cls.render = staticmethod(renderer["render"])
        cls.concise_message = staticmethod(renderer["concise_message"])

    def data(self, after=None):
        agent_op = "a" * 64
        row = {
            "number": 1,
            "agent_op": agent_op,
            "agent_completed_at": "2026-01-01T00:00:03.000000",
            "agent_duration": "0:03",
            "after_count": 7,
            "delta": -3,
            "tokens_used": 70,
            "final_message": "Implemented one safety iteration.\n\nRemoved three unsafe operations.\n\nVerification passed:\n- tests",
            "result": "accepted",
            "rejection_reason": None,
            "test_exit_code": 0,
        }
        return {
            "rows": [row],
            "aggregate": {
                "completed_edits": 1,
                "accepted_edits": 1,
                "rejected_edits": 0,
                "total_tokens_used": 70,
                "mean_tokens_per_completed_edit": 70,
                "median_tokens_per_completed_edit": 70,
                "total_agent_runtime": "0:03",
                "initial_unsafe_count": 10,
                "final_unsafe_count": 7,
                "net_unsafe_removed": 3,
                "internal_check_unsafe2_increase_count": 0,
            },
            "selection": {
                "after": after,
                "agent_op": None,
                "returned_rows": 1,
            },
            "checkpoint": {"last_agent_op": agent_op},
            "coverage_warnings": [],
        }

    def test_renders_complete_history(self):
        output = self.render(self.data(), "example")
        self.assertIn("# example CRISP run summary", output)
        self.assertIn("`aaaaaaaaaaaa`", output)
        self.assertIn("Removed three unsafe operations.", output)
        self.assertNotIn("Verification passed", output)

    def test_rejects_filtered_history(self):
        with self.assertRaisesRegex(ValueError, "complete summary"):
            self.render(self.data(after="a" * 64), "example")

    def test_cleans_inline_session_prose(self):
        message = (
            "**Completed** Replaced a raw parser with a safe slice helper. "
            "Updated `SAFETY_PLAN.md:1` with the next target. "
            "**Validation** `cargo check-unsafe2` passes. Tests pass."
        )
        self.assertEqual(
            self.concise_message(message),
            "Replaced a raw parser with a safe slice helper.",
        )

    def test_cleans_path_prefixed_plan_prose(self):
        message = (
            "Simplified `parse_string` to the raw allocation boundary. "
            "`rust/src/cJSON.rs:852` Updated iteration notes and identified "
            "the next migration target. `SAFETY_PLAN.md:42`"
        )
        self.assertEqual(
            self.concise_message(message),
            "Simplified `parse_string` to the raw allocation boundary.",
        )

    def test_removes_compacted_plan_bullet_without_fragment(self):
        message = (
            "**Completed**\n"
            "- Removed obsolete unsafe imports.\n"
            "- Compacted and updated continuing notes in `SAFETY_PLAN.md:1`."
        )
        self.assertEqual(
            self.concise_message(message),
            "Removed obsolete unsafe imports.",
        )

    def test_preserves_technical_updated_sentence_and_zero_delta_outcome(self):
        message = (
            "**Iteration Result** Updated object-key lookup to use safe "
            "`CStr` comparison. No Rust source change was retained for the "
            "allocator experiment."
        )
        self.assertEqual(
            self.concise_message(message),
            "Updated object-key lookup to use safe `CStr` comparison. "
            "No Rust source change was retained for the allocator experiment.",
        )

    def test_long_summary_stops_at_complete_sentence(self):
        message = "First technical change. " + ("Detailed behavior text " * 40)
        summary = self.concise_message(message)
        self.assertLessEqual(len(summary), 450)
        self.assertTrue(summary.endswith("."))
        self.assertFalse(summary.endswith("..."))

    def test_validates_aggregate_counts(self):
        data = self.data()
        data["aggregate"]["completed_edits"] = 2
        with self.assertRaisesRegex(ValueError, "completed_edits"):
            self.render(data, "example")

    def test_validates_final_count_and_checkpoint(self):
        data = self.data()
        data["aggregate"]["final_unsafe_count"] = 8
        with self.assertRaisesRegex(ValueError, "final row unsafe count"):
            self.render(data, "example")

        data = self.data()
        data["checkpoint"]["last_agent_op"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "checkpoint"):
            self.render(data, "example")


if __name__ == "__main__":
    unittest.main()
