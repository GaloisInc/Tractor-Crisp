"""Build structured safety-loop history from CRISP's MVIR."""

from __future__ import annotations

from datetime import datetime
import json
import re
from statistics import mean, median
from typing import Any

from .mvir import MVIR, NodeId


AGENT_KIND = "codex_agent_op_v2"
FIND_UNSAFE_KIND = "find_unsafe2_analysis"
CHECK_UNSAFE_KIND = "check_unsafe2_analysis"
TEST_KIND = "test_result_node"
CHECK_COMMAND_RE = re.compile(r"\bcargo\s+check-unsafe2\b")
FIND_COMMAND_RE = re.compile(r"\bcargo\s+find-unsafe2\b")
INCREASE_RE = re.compile(r"\bincreased:\s*\d+\s*->\s*\d+")


def _node_id(value: Any) -> str:
    return str(value.node_id() if hasattr(value, "node_id") else value)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format_duration(seconds: float | int | None) -> str | None:
    if seconds is None:
        return None
    total = max(0, round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _reflog(mvir: MVIR, tag: str) -> list[Any]:
    return list(mvir.tag_reflog(tag)) if mvir.has_tag(tag) else []


def _safety_reason(reason: Any) -> bool:
    return (
        isinstance(reason, (list, tuple))
        and len(reason) >= 2
        and list(reason[:2]) == ["main", "safety"]
    )


def _indexed_nodes(mvir: MVIR, node_id: NodeId, kind: str, key: str) -> list[Any]:
    return [
        mvir.node(entry.node_id)
        for entry in mvir.index(node_id)
        if entry.kind == kind and entry.key == key
    ]


def _unsafe_count_from_tree(mvir: MVIR, tree_id: NodeId) -> int:
    tree = mvir.node(tree_id)
    return sum(
        int(mvir.node(file_id).body_json().get("total_unsafe", 0))
        for file_id in tree.files.values()
    )


def _find_unsafe(mvir: MVIR, code_id: NodeId) -> tuple[Any | None, int | None]:
    nodes = [
        node
        for node in _indexed_nodes(mvir, code_id, FIND_UNSAFE_KIND, "code")
        if node.exit_code == 0
    ]
    if not nodes:
        return None, None
    node = nodes[0]
    return node, _unsafe_count_from_tree(mvir, node.unsafe_json)


def _session_events(mvir: MVIR, op: Any) -> tuple[str, list[dict[str, Any]]]:
    session_id = _node_id(op.json_session)
    body = mvir.node(op.json_session).body_str()
    events = []
    for line in body.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return session_id, events


def _session_info(
    events: list[dict[str, Any]],
    include_internal_output: bool = False,
    include_agent_commands: bool = False,
) -> dict[str, Any]:
    timestamps = [_parse_iso(event.get("timestamp")) for event in events]
    timestamps = [value for value in timestamps if value is not None]
    final_message = None
    duration_seconds = None
    final_usage = None
    calls: dict[str, dict[str, Any]] = {}
    call_timestamps: dict[str, str | None] = {}
    outputs: dict[str, str] = {}

    for event in events:
        payload = event.get("payload") or {}
        event_type = event.get("type")
        payload_type = payload.get("type")

        if event_type == "event_msg" and payload_type == "token_count":
            usage = ((payload.get("info") or {}).get("total_token_usage"))
            if isinstance(usage, dict):
                final_usage = usage
        if event_type == "event_msg" and payload_type == "task_complete":
            final_message = payload.get("last_agent_message") or final_message
            if payload.get("duration_ms") is not None:
                duration_seconds = payload["duration_ms"] / 1000
        if (
            event_type == "event_msg"
            and payload_type == "agent_message"
            and payload.get("phase") == "final_answer"
        ):
            final_message = payload.get("message") or final_message
        if (
            event_type == "response_item"
            and payload_type == "message"
            and payload.get("phase") == "final_answer"
        ):
            texts = [
                part.get("text", "")
                for part in payload.get("content", [])
                if part.get("type") == "output_text"
            ]
            if texts:
                final_message = "\n".join(texts)
        if event_type == "response_item" and payload_type == "function_call":
            call_id = payload.get("call_id")
            if call_id:
                calls[call_id] = payload
                call_timestamps[call_id] = event.get("timestamp")
        if event_type == "response_item" and payload_type == "function_call_output":
            call_id = payload.get("call_id")
            if call_id:
                outputs[call_id] = str(payload.get("output", ""))

    if duration_seconds is None and len(timestamps) >= 2:
        duration_seconds = (timestamps[-1] - timestamps[0]).total_seconds()

    tokens_used = None
    if final_usage is not None:
        input_tokens = int(final_usage.get("input_tokens", 0))
        cached_tokens = int(final_usage.get("cached_input_tokens", 0))
        output_tokens = int(final_usage.get("output_tokens", 0))
        tokens_used = input_tokens - cached_tokens + output_tokens

    check_runs = []
    find_runs = []
    agent_commands = []
    for call_id, payload in calls.items():
        if payload.get("name") not in {"exec_command", "functions.exec_command"}:
            continue
        arguments = payload.get("arguments", "")
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError:
            args = {}
        command = str((args or {}).get("cmd", ""))
        if include_agent_commands:
            agent_commands.append({
                "timestamp": call_timestamps.get(call_id),
                "call_id": call_id,
                "command": command,
            })
        # A heredoc body may merely mention a command in prose. Only its first
        # line is shell syntax when deciding whether this tool call invokes it.
        command_syntax = command.splitlines()[0] if "<<" in command else command
        output = outputs.get(call_id, "")
        if FIND_COMMAND_RE.search(command_syntax) and "--help" not in command_syntax:
            find_run = {"call_id": call_id, "command": command}
            if include_internal_output:
                find_run["output"] = output
            find_runs.append(find_run)
        if not CHECK_COMMAND_RE.search(command_syntax) or "--help" in command_syntax:
            continue
        increase_lines = [
            line.strip() for line in output.splitlines() if INCREASE_RE.search(line)
        ]
        check_run = {
            "call_id": call_id,
            "command": command,
            "reported_increase": bool(increase_lines),
            "increase_lines": increase_lines,
        }
        if include_internal_output:
            check_run["output"] = output
        check_runs.append(check_run)

    return {
        "started_at": _iso(timestamps[0]) if timestamps else None,
        "ended_at": _iso(timestamps[-1]) if timestamps else None,
        "duration_seconds": duration_seconds,
        "duration": _format_duration(duration_seconds),
        "tokens_used": tokens_used,
        "final_message": final_message,
        "internal_check_unsafe2_count": len(check_runs),
        "internal_check_unsafe2_increase_count": sum(
            run["reported_increase"] for run in check_runs
        ),
        "internal_check_unsafe2_runs": check_runs,
        "internal_find_unsafe2_count": len(find_runs),
        "internal_find_unsafe2_runs": find_runs,
        "agent_commands": agent_commands if include_agent_commands else None,
    }


def _validation_nodes(
    mvir: MVIR,
    op: Any,
    start: datetime,
    end: datetime | None,
    test_log: list[Any],
) -> tuple[Any | None, Any | None]:
    tests_in_interval = []
    for entry in test_log:
        if entry.timestamp < start or (end is not None and entry.timestamp >= end):
            continue
        node = mvir.node(entry.node_id)
        if _node_id(node.code) == _node_id(op.new_code):
            tests_in_interval.append(node)
    if tests_in_interval:
        test_node = tests_in_interval[0]
    else:
        indexed_tests = _indexed_nodes(mvir, op.new_code, TEST_KIND, "code")
        test_node = indexed_tests[0] if indexed_tests else None

    old_find, _ = _find_unsafe(mvir, op.old_code)
    baseline_ids = {_node_id(old_find.unsafe_json)} if old_find is not None else set()
    checks = _indexed_nodes(mvir, op.new_code, CHECK_UNSAFE_KIND, "code")
    matched_checks = [
        node for node in checks if _node_id(node.unsafe_json) in baseline_ids
    ]
    check_node = matched_checks[0] if matched_checks else None
    return test_node, check_node


def _diagnostics(body: str) -> list[str]:
    needles = (
        "increased:", "panicked at", "missing field", "error:", "failed",
        "Traceback",
    )
    lines = [
        line.strip()
        for line in body.splitlines()
        if any(needle in line for needle in needles)
    ]
    return lines[-20:]


def _unsafe_rejection_reason(
    body: str, exit_code: int, diagnostics: list[str]
) -> str:
    increased = [line for line in body.splitlines() if "increased:" in line]
    if increased:
        return increased[0].strip()
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if "panicked at" not in line:
            continue
        detail = next((item.strip() for item in lines[index + 1:] if item.strip()), "")
        return f"unsafe check panicked: {detail}" if detail else line.strip()
    return (
        diagnostics[-1]
        if diagnostics
        else f"unsafe check failed with exit code {exit_code}"
    )


def build_safety_history(
    mvir: MVIR,
    after: NodeId | None = None,
    agent_op: NodeId | None = None,
    include_internal_output: bool = False,
    include_agent_commands: bool = False,
) -> dict[str, Any]:
    """Return JSON-serializable history for completed Codex safety turns."""
    if after is not None and agent_op is not None:
        raise ValueError("after and agent_op are mutually exclusive")
    op_log = _reflog(mvir, "op_history")
    agent_entries = [
        entry for entry in op_log if mvir.node(entry.node_id).kind == AGENT_KIND
    ]
    current_log = [
        entry for entry in _reflog(mvir, "current") if _safety_reason(entry.reason)
    ]
    test_log = _reflog(mvir, "test_results")
    rows = []
    warnings = []

    for index, entry in enumerate(agent_entries):
        op = mvir.node(entry.node_id)
        next_time = (
            agent_entries[index + 1].timestamp
            if index + 1 < len(agent_entries)
            else None
        )
        accept_entries = [
            accepted
            for accepted in current_log
            if accepted.timestamp >= entry.timestamp
            and (next_time is None or accepted.timestamp < next_time)
            and _node_id(accepted.node_id) == _node_id(op.new_code)
        ]
        accepted = bool(accept_entries)
        accepted_at = accept_entries[0].timestamp if accept_entries else None

        _, before_count = _find_unsafe(mvir, op.old_code)
        _, candidate_count = _find_unsafe(mvir, op.new_code)
        after_count = candidate_count if accepted else before_count
        delta = (
            None
            if before_count is None or after_count is None
            else after_count - before_count
        )

        test_node, check_node = _validation_nodes(
            mvir, op, entry.timestamp, next_time, test_log
        )
        session_id, events = _session_events(mvir, op)
        session = _session_info(
            events,
            include_internal_output=(
                include_internal_output
                and (agent_op is None or _node_id(entry.node_id) == _node_id(agent_op))
            ),
            include_agent_commands=(
                include_agent_commands
                and (agent_op is None or _node_id(entry.node_id) == _node_id(agent_op))
            ),
        )

        test_exit = test_node.exit_code if test_node is not None else None
        check_exit = check_node.exit_code if check_node is not None else None
        check_diagnostics = (
            _diagnostics(check_node.body_str()) if check_node is not None else []
        )
        if accepted:
            rejection_reason = None
        elif test_exit not in (None, 0):
            rejection_reason = f"tests failed with exit code {test_exit}"
        elif check_exit not in (None, 0):
            rejection_reason = _unsafe_rejection_reason(
                check_node.body_str(), check_exit, check_diagnostics
            )
        elif check_node is None:
            rejection_reason = (
                "rejected; unsafe-check evidence is unavailable in MVIR"
            )
        else:
            rejection_reason = "rejected by CRISP validation"

        if not events:
            warnings.append(
                f"agent op {_node_id(entry.node_id)} has no parseable JSON session"
            )
        if before_count is None:
            warnings.append(
                f"agent op {_node_id(entry.node_id)} has no stored unsafe count for old code"
            )
        if accepted and candidate_count is None:
            warnings.append(
                f"accepted agent op {_node_id(entry.node_id)} has no stored unsafe count for new code"
            )

        rows.append({
            "number": index + 1,
            "agent_op": _node_id(entry.node_id),
            "agent_completed_at": _iso(entry.timestamp),
            "agent_started_at": session["started_at"],
            "agent_duration_seconds": session["duration_seconds"],
            "agent_duration": session["duration"],
            "old_code": _node_id(op.old_code),
            "candidate_code": _node_id(op.new_code),
            "accepted_code": _node_id(op.new_code if accepted else op.old_code),
            "accepted_at": _iso(accepted_at),
            "result": "accepted" if accepted else "rejected",
            "rejection_reason": rejection_reason,
            "before_count": before_count,
            "candidate_count": candidate_count,
            "after_count": after_count,
            "delta": delta,
            "tokens_used": session["tokens_used"],
            "final_message": session["final_message"],
            "json_session": session_id,
            "test_node": _node_id(test_node) if test_node is not None else None,
            "test_exit_code": test_exit,
            "unsafe_check_node": (
                _node_id(check_node) if check_node is not None else None
            ),
            "unsafe_check_exit_code": check_exit,
            "unsafe_check_diagnostics": check_diagnostics,
            "internal_check_unsafe2_count": (
                session["internal_check_unsafe2_count"]
            ),
            "internal_check_unsafe2_increase_count": (
                session["internal_check_unsafe2_increase_count"]
            ),
            "internal_check_unsafe2_runs": (
                session["internal_check_unsafe2_runs"]
            ),
            "internal_find_unsafe2_count": (
                session["internal_find_unsafe2_count"]
            ),
            "internal_find_unsafe2_runs": (
                session["internal_find_unsafe2_runs"]
            ),
            **(
                {"agent_commands": session["agent_commands"]}
                if session["agent_commands"] is not None
                else {}
            ),
        })

    all_rows = rows
    selected_agent_op = agent_op if agent_op is not None else after
    if selected_agent_op is not None:
        matches = [
            i for i, row in enumerate(rows)
            if row["agent_op"] == str(selected_agent_op)
        ]
        if not matches:
            raise ValueError(
                f"agent operation {selected_agent_op} is not present in safety history"
            )
        if agent_op is not None:
            rows = [rows[matches[-1]]]
        else:
            rows = rows[matches[-1] + 1:]

    token_values = [
        row["tokens_used"] for row in all_rows if row["tokens_used"] is not None
    ]
    duration_values = [
        row["agent_duration_seconds"]
        for row in all_rows
        if row["agent_duration_seconds"] is not None
    ]
    accepted_rows = [row for row in all_rows if row["result"] == "accepted"]
    rejected_rows = [row for row in all_rows if row["result"] == "rejected"]
    initial_count = all_rows[0]["before_count"] if all_rows else None
    final_count = all_rows[-1]["after_count"] if all_rows else None

    return {
        "schema_version": 1,
        "rows": rows,
        "aggregate": {
            "completed_edits": len(all_rows),
            "accepted_edits": len(accepted_rows),
            "rejected_edits": len(rejected_rows),
            "total_tokens_used": sum(token_values),
            "mean_tokens_per_completed_edit": (
                mean(token_values) if token_values else None
            ),
            "median_tokens_per_completed_edit": (
                median(token_values) if token_values else None
            ),
            "total_agent_duration_seconds": sum(duration_values),
            "total_agent_runtime": _format_duration(sum(duration_values)),
            "initial_unsafe_count": initial_count,
            "final_unsafe_count": final_count,
            "net_unsafe_removed": (
                None
                if initial_count is None or final_count is None
                else initial_count - final_count
            ),
            "internal_check_unsafe2_count": sum(
                row["internal_check_unsafe2_count"] for row in all_rows
            ),
            "internal_check_unsafe2_increase_count": sum(
                row["internal_check_unsafe2_increase_count"] for row in all_rows
            ),
        },
        "selection": {
            "after": str(after) if after is not None else None,
            "agent_op": str(agent_op) if agent_op is not None else None,
            "returned_rows": len(rows),
        },
        "checkpoint": {
            "last_agent_op": all_rows[-1]["agent_op"] if all_rows else None,
            "last_agent_completed_at": (
                all_rows[-1]["agent_completed_at"] if all_rows else None
            ),
        },
        "coverage_warnings": sorted(set(warnings)),
    }
