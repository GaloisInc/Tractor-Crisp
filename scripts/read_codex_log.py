#!/usr/bin/env python3
"""Render a Codex JSONL session: python3 scripts/read_codex_log.py < log.json.

Uses only the standard library and processes one record at a time. Response
items are authoritative; duplicate UI events and runtime metadata are omitted.
Encrypted content cannot be decoded and is represented by a placeholder.
"""

import argparse
import json
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
    if text:
        print(f"<{role}>\n{text}\n")


class Formatter:
    def __init__(self):
        self.reasoning_ids = set()

    def reasoning(self, item):
        # A reasoning item may occur as both a response item and a UI event.
        text = readable(item.get("content") or item.get("raw_content"))
        summary = readable(item.get("summary") or item.get("summary_text"))
        body = "\n".join(filter(None, (text, summary)))
        if not body and item.get("encrypted_content"):
            body = "[encrypted reasoning unavailable]"
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
            emit("assistant", f"[tool call: {name}]")
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
