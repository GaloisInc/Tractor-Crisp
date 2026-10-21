#!/usr/bin/env python3
"""
Render a Codex JSONL session:

    python3 scripts/read_codex_log.py < log.json


To read the logs of a `CodexAgentOpNode` `aabb1122`, run:

    crisp show --body aabb1122.json_session | \
        python3 /path/to/tractor-crisp/scripts/read_codex_log.py

This is usually more readable than `crisp show --body aabb1122`, which shows
the stdout/stderr from the `codex` run, because Codex often re-prints the same
output (such as diffs or command results) many times during its run.


If the previous command returns no output, then `aabb1122.json_session` may be
empty because there were multiple session logs found in the agent sandbox (this
happens when using subagents).  In this case, you can instead run this command
to see all of the logs (among other outputs):

    crisp show aabb1122.raw_output_files

Output should include entries like:

    '.codex/sessions/2026/10/21/rollout-2026-10-21T21-51-53-[...].jsonl': NodeId(ccdd3344...),

You can then view the log with

    crisp show --body ccdd3344 | \
        python3 /path/to/tractor-crisp/scripts/read_codex_log.py
"""

import argparse
import json
import re
import sys


def readable(content):
    """Extract text without dumping opaque content or embedded media."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(filter(None, (readable(part) for part in content)))
    if not isinstance(content, dict):
        return ""
    if isinstance(content.get("text"), str):
        return content["text"]
    kind = content.get("type", "").lower()
    if kind == "encrypted_content":
        return "[encrypted content unavailable]"
    if "image" in kind:
        return "[image]"
    if "audio" in kind:
        return "[audio]"
    return ""


def emit(role, text):
    if text and text.strip() != "[encrypted reasoning unavailable]":
        print(f"<{role}>\n{text}\n")


def tool_detail(item):
    """Return a bounded preview; never execute code from a tool call."""
    raw = item.get("arguments", item.get("input", ""))
    if isinstance(raw, str):
        try:
            args = json.loads(raw)
        except ValueError:
            args = raw
    else:
        args = raw

    detail = ""
    if isinstance(args, dict):
        for field in ("cmd", "command", "path", "file_path", "filename",
                      "query", "url", "task_name", "target"):
            if args.get(field):
                value = args[field]
                if isinstance(value, list):
                    value = " ".join(map(str, value))
                detail = f"{field}: {value}"
                break
        if not detail:
            # Some tools wrap a patch or script in a JSON argument.
            detail = str(args.get("patch", args.get("code", "")))
    elif isinstance(args, str):
        detail = args

    # Prefer filenames over the body of an apply_patch request.
    paths = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", detail, re.M)
    if paths:
        detail = ", ".join(paths)
    else:
        # The exec wrapper often contains JavaScript invoking exec_command.
        commands = []
        for match in re.finditer(
            r'''(?:["']?\b(?:cmd|command)["']?)\s*:\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''',
            detail,
        ):
            quoted = match.group(1)
            try:
                command = json.loads(quoted) if quoted.startswith('"') else quoted[1:-1]
            except ValueError:
                command = quoted[1:-1]
            commands.append(command)
        if commands:
            detail = "; ".join(commands)

    # Uncomment this to reduce tool call details to one line and truncate them.
    #detail = " ".join(detail.split())
    #return detail[:157] + "..." if len(detail) > 160 else detail
    return detail


class Formatter:
    def __init__(self):
        self.reasoning_ids = set()

    def reasoning(self, item):
        # A reasoning item may occur as both a response item and a UI event.
        text = readable(item.get("content") or item.get("raw_content"))
        summary = readable(item.get("summary") or item.get("summary_text"))
        body = "\n".join(filter(None, (text, summary)))
        if body.strip() == "[encrypted reasoning unavailable]":
            return
        key = item.get("id")
        if body and (not key or key not in self.reasoning_ids):
            emit("assistant thinking", body)
            if key:
                self.reasoning_ids.add(key)

    def record(self, record):
        kind = record.get("type")
        item = record.get("payload", {})
        if not isinstance(item, dict):
            return
        if kind == "session_meta":
            instructions = item.get("base_instructions", "")
            emit("system", readable(instructions))
        elif kind == "response_item":
            self.response(item)
        elif kind == "event_msg" and item.get("type") == "item_completed":
            completed = item.get("item", {})
            if isinstance(completed, dict) and completed.get("type") == "Reasoning":
                self.reasoning(completed)
        elif kind == "compacted":
            emit("summary", readable(item.get("message")))

    def response(self, item):
        kind = item.get("type", "")
        if kind == "message":
            role = item.get("role", "unknown")
            if role == "tool":
                emit("tool", "[result omitted]")
            else:
                emit(role, readable(item.get("content")))
        elif kind == "reasoning":
            self.reasoning(item)
        elif kind in {"function_call", "custom_tool_call"} or kind.endswith("_call"):
            name = item.get("name") or kind.removesuffix("_call")
            namespace = item.get("namespace")
            if namespace:
                name = f"{namespace}.{name}"
            detail = tool_detail(item)
            suffix = f" — {detail}" if detail else ""
            emit("assistant", f"[tool call: {name}{suffix}]")
        elif kind.endswith("_call_output"):
            # The call itself is enough to show the interaction.
            pass
        elif kind == "agent_message":
            author = item.get("author", "unknown")
            recipient = item.get("recipient", "unknown")
            emit(f"agent {author} -> {recipient}", readable(item.get("content")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    formatter = Formatter()
    for line_number, line in enumerate(sys.stdin, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"Invalid JSON on line {line_number}: {exc.msg}", file=sys.stderr)
            return 1
        if not isinstance(record, dict):
            print(f"Expected an object on line {line_number}", file=sys.stderr)
            return 1
        formatter.record(record)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Allow piping the transcript into head or a pager that exits early.
        sys.stdout = open('/dev/null', 'w')
        sys.exit(0)
