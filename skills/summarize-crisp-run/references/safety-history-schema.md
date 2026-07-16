# `crisp safety-history` Schema

The command emits one JSON object with these top-level fields:

- `schema_version`: output schema version.
- `rows`: completed Codex agent operations, chronologically ordered. With `--after`, contains only rows after the exclusive checkpoint. With `--agent-op`, contains exactly the selected globally numbered row.
- `aggregate`: totals for the complete history, even when `rows` is filtered by `--after`.
- `selection`: requested `after` or `agent_op` selector and number of returned rows.
- `checkpoint`: final agent-operation ID and timestamp in the complete history.
- `coverage_warnings`: missing session or unsafe-count evidence.

Important row fields:

- Identity: `number`, `agent_op`, `agent_completed_at`.
- Code: `old_code`, `candidate_code`, `accepted_code`.
- Result: `result`, `accepted_at`, `rejection_reason`.
- Safety: `before_count`, `candidate_count`, `after_count`, `delta`.
- Agent session: `tokens_used`, `agent_duration`, `final_message`, `json_session`.
- CRISP validation: `test_node`, `test_exit_code`, `unsafe_check_node`, `unsafe_check_exit_code`, `unsafe_check_diagnostics`.
- Internal experiments: `internal_check_unsafe2_count`, `internal_check_unsafe2_increase_count`, `internal_check_unsafe2_runs`, `internal_find_unsafe2_count`, `internal_find_unsafe2_runs`.

Each `internal_check_unsafe2_runs` entry contains the tool-call ID, command,
whether an increase was reported, and matching diagnostic lines. With
`--include-internal-output`, it also contains the complete stored tool output
in `output`; this is opt-in because full-run output can be large.
The corresponding `internal_find_unsafe2_*` fields expose agent-side report
generation, which can overwrite the comparison baseline when both commands use
the same JSON directory.

With `--include-agent-commands`, a row also has `agent_commands`, containing the
chronological stored shell commands with their call IDs and timestamps.

For rejected rows, `candidate_count` can be null because CRISP compares the candidate against the old unsafe report without necessarily running a standalone unsafe-count analysis on the rejected code. `after_count` remains the old accepted count and `delta` is zero.

`tokens_used` is reconstructed as:

```text
input_tokens - cached_input_tokens + output_tokens
```

`agent_duration` comes from the stored session's `task_complete.duration_ms`, with the first and last event timestamps as a fallback.
