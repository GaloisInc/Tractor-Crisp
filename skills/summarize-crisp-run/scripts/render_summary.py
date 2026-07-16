#!/usr/bin/env python3
"""Render `crisp safety-history` JSON from stdin as Markdown."""

from __future__ import annotations

import argparse
import json
import re
from statistics import mean, median
import sys


SECTION_MARKER = re.compile(
    r"(?:\n|\s)+(?:(?:I also |and )?[Uu]pdated|Created/updated) SAFETY_PLAN|"
    r"(?:\n|\s)+\*\*(?:Validation|Validated|Verified|Plan|Next(?: Iteration)?)\*\*|"
    r"\n+Verification(?: passed)?:|\n+Verified with:|"
    r"\n+Tests passed|\n+Workspace|\n+Only the|\n+Note:",
    re.I,
)

SESSION_SENTENCE = re.compile(
    r"^(?:Validation|Validated|Verified|Tests?|Workspace|Existing warnings|"
    r"The checker reports|The generated|Test execution|Left (?:the )?pre-existing|"
    r"Removed the generated|Only `SAFETY_PLAN|The next iteration)",
    re.I,
)

PLAN_SENTENCE = re.compile(
    r"^(?:Updated|Recorded|Compacted|Queued|Selected)", re.I
)

PLAN_TERM = re.compile(
    r"\b(?:iteration|continuation|progress|handoff|next|plan|notes|target)\b",
    re.I,
)


def _shorten_at_sentence(text, limit=450):
    if len(text) <= limit:
        return text
    shortened = text[:limit]
    sentence_end = max(shortened.rfind(". "), shortened.rfind("! "),
                       shortened.rfind("? "))
    if sentence_end >= 0:
        return shortened[:sentence_end + 1]
    return shortened.rsplit(" ", 1)[0] + "..."


def concise_message(message):
    if not message:
        return "Final agent summary unavailable from the stored session."
    text = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", message)
    text = SECTION_MARKER.split(text, maxsplit=1)[0]
    text = re.sub(r"^\*\*[^*]+\*\*\s*", "", text)
    paragraphs = [
        part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()
    ]
    kept = []
    stop_prefixes = (
        "Updated SAFETY_PLAN", "Created/updated SAFETY_PLAN", "Verification",
        "Tests passed", "Workspace", "Only the", "Note:",
    )
    generic = re.compile(
        r"^(Implemented|Completed|Finished) (one |this )?(scoped )?"
        r"(safe[- ]|safety[- ]|unsafe-reduction )?"
        r"(refactor (target|iteration)|iteration|pass)"
        r"(?: in \S+)?\.?(?:\s+|$)",
        re.I,
    )
    for paragraph in paragraphs:
        if paragraph.startswith(stop_prefixes):
            break
        paragraph = generic.sub("", paragraph)
        if not paragraph:
            continue
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if lines and lines[0].startswith((
            "Changed rust/", "Implemented in rust/",
            "Implemented this iteration in rust/",
        )):
            lines[0] = re.sub(
                r"^(Changed|Implemented(?: this iteration)? in) rust/[^:]+:\s*",
                "",
                lines[0],
            )
            if not lines[0]:
                lines = lines[1:]
        paragraph = " ".join(
            re.sub(r"^-\s*", "", line) for line in lines
        )
        if paragraph:
            kept.append(paragraph)
    summary = " ".join(kept).replace("|", "\\|")
    summary = re.sub(r"\s+", " ", summary).strip()

    cleaned_sentences = []
    for sentence in re.split(r"(?<=[.!?])\s+", summary):
        if "SAFETY_PLAN.md" in sentence:
            if PLAN_SENTENCE.match(sentence):
                continue
            plan_start = re.search(
                r"\s+(?:Added|Updated|Recorded|Compacted|Queued|Selected)\b",
                sentence,
                re.I,
            )
            if plan_start:
                sentence = sentence[:plan_start.start()].strip()
            else:
                continue
        if not sentence or SESSION_SENTENCE.match(sentence):
            continue
        plan_sentence = re.sub(r"^`?rust/[^`\s]+`?\s+", "", sentence)
        if PLAN_SENTENCE.match(plan_sentence) and PLAN_TERM.search(plan_sentence):
            continue
        if re.fullmatch(r"`?rust/[^`\s]+`?", sentence):
            continue
        cleaned_sentences.append(sentence)
    summary = " ".join(cleaned_sentences)

    bare_names = re.match(r"^((?:`[^`]+`\s*)+)Their bodies\b", summary)
    if bare_names:
        names = re.findall(r"`[^`]+`", bare_names.group(1))
        joined = (
            names[0]
            if len(names) == 1
            else ", ".join(names[:-1]) + " and " + names[-1]
        )
        summary = (
            f"Removed the implementation trampolines for {joined}. Their bodies"
            + summary[bare_names.end():]
        )
    if summary:
        summary = summary[0].upper() + summary[1:]
    summary = _shorten_at_sentence(summary)
    return summary or "Completed the returned safety edit."


def validate_summary_data(data):
    rows = data["rows"]
    aggregate = data["aggregate"]
    checkpoint = data["checkpoint"]
    completed = aggregate["completed_edits"]
    if completed != len(rows):
        raise ValueError(
            f"aggregate completed_edits is {completed}, but history has "
            f"{len(rows)} rows"
        )

    accepted = sum(row["result"] == "accepted" for row in rows)
    rejected = sum(row["result"] == "rejected" for row in rows)
    if accepted != aggregate["accepted_edits"]:
        raise ValueError("aggregate accepted_edits does not match rows")
    if rejected != aggregate["rejected_edits"]:
        raise ValueError("aggregate rejected_edits does not match rows")
    if accepted + rejected != len(rows):
        raise ValueError("history contains an unknown edit result")

    if rows:
        if rows[-1]["after_count"] != aggregate["final_unsafe_count"]:
            raise ValueError("final row unsafe count does not match aggregate")
        if rows[-1]["agent_op"] != checkpoint["last_agent_op"]:
            raise ValueError("final row agent operation does not match checkpoint")

    prefixes = [row["agent_op"][:12] for row in rows]
    if len(prefixes) != len(set(prefixes)):
        raise ValueError("12-character agent operation prefixes are not unique")


def render(data, run_name):
    validate_summary_data(data)
    rows = data["rows"]
    aggregate = data["aggregate"]
    selection = data.get("selection", {})
    if selection.get("after") is not None or selection.get("agent_op") is not None:
        raise ValueError("render a complete summary from unfiltered history")
    deltas = [row["delta"] for row in rows if row["delta"] is not None]
    all_tests_passed = all(row["test_exit_code"] == 0 for row in rows)
    warnings = data.get("coverage_warnings") or []

    out = [
        f"# {run_name} CRISP run summary",
        "",
        "This summary was generated from CRISP's MVIR through `crisp safety-history`. Each row is one completed `CodexAgentOpNode`: the final edit returned by an agent invocation, rather than intermediate edits attempted within that turn.",
        "",
        f"- Total accepted edits: {aggregate['accepted_edits']}",
        f"- Total rejected edits: {aggregate['rejected_edits']}",
        f"- Total tokens used: {aggregate['total_tokens_used']:,}",
        f"- Mean tokens per completed CRISP-level row: {aggregate['mean_tokens_per_completed_edit']:,.0f}",
        f"- Median tokens per completed CRISP-level row: {aggregate['median_tokens_per_completed_edit']:,}",
        f"- Total agent runtime: {aggregate['total_agent_runtime']}",
        f"- Initial unsafe count: {aggregate['initial_unsafe_count']:,}",
        f"- Final unsafe count: {aggregate['final_unsafe_count']:,}",
        f"- Net unsafe operations removed by accepted edits: {aggregate['net_unsafe_removed']:,}",
    ]
    if deltas:
        out.append(
            "- Average unsafe delta per completed CRISP-level row, including "
            f"rejected rows: mean `{mean(deltas):.2f}`, median `{median(deltas):g}`"
        )
    out.extend([
        "- Agent-internal `cargo check-unsafe2` runs reporting increases: "
        f"{aggregate['internal_check_unsafe2_increase_count']}",
        "- Last processed MVIR agent operation: "
        f"`{data['checkpoint']['last_agent_op']}`",
        "",
    ])

    rejected = [row for row in rows if row["result"] == "rejected"]
    if rejected:
        details = "; ".join(
            f"row {row['number']}: {row['rejection_reason']}" for row in rejected
        )
        out.extend([
            f"CRISP rejected {len(rejected)} returned edit(s): {details}. "
            "Rejected candidates remain represented by their agent-operation "
            "nodes and do not advance the accepted unsafe count.",
            "",
        ])
    if all_tests_passed:
        out.extend([
            "Every stored CRISP-level test result for these completed rows has exit code 0.",
            "",
        ])
    if warnings:
        out.extend(["MVIR coverage warnings: " + "; ".join(warnings) + ".", ""])

    out.extend([
        "`Duration` is the exact Codex task duration stored in each JSON session. It excludes CRISP's post-agent test and unsafe-comparison time, so it is intentionally not the legacy log-derived completed-step runtime.",
        "",
        "| # | Agent operation | Duration | Unsafe count | Delta | Tokens used | Final edit summary | Result |",
        "|---:|---|---:|---:|---:|---:|---|---|",
    ])
    for row in rows:
        completed = row["agent_completed_at"].replace("T", " ").split(".", 1)[0]
        operation = f"`{row['agent_op'][:12]}`<br>{completed}"
        result = (
            "accepted"
            if row["result"] == "accepted"
            else f"rejected: {row['rejection_reason']}"
        ).replace("|", "\\|")
        out.append(
            f"| {row['number']} | {operation} | {row['agent_duration'] or ''} | "
            f"{'' if row['after_count'] is None else row['after_count']} | "
            f"{'' if row['delta'] is None else row['delta']} | "
            f"{'' if row['tokens_used'] is None else format(row['tokens_used'], ',')} | "
            f"{concise_message(row['final_message'])} | {result} |"
        )
    out.append("")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-name", required=True)
    args = parser.parse_args()
    data = json.load(sys.stdin)
    print(render(data, args.run_name))


if __name__ == "__main__":
    main()
