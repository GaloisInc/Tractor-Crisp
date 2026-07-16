---
name: summarize-crisp-run
description: Build or incrementally update a CRISP safety-loop SUMMARY.md from the structured `crisp safety-history` CLI output. Use for CRISP experiment reports covering accepted and rejected returned edits, unsafe-count deltas, token and agent-runtime totals, validation results, and resumed runs.
---

# Summarize a CRISP Run

Treat one summary row as one completed Codex agent operation returned to CRISP. Include accepted and rejected returned edits; exclude invocations that never produced an agent-operation node.

## Generate a Fresh Summary

Work from the experiment directory containing `crisp.toml`. Pipe the CLI's JSON directly into the bundled Markdown renderer so the only created file is the requested summary:

```sh
uv run --project /path/to/Tractor-Crisp crisp safety-history --compact \
  | python3 -B /path/to/Tractor-Crisp/skills/summarize-crisp-run/scripts/render_summary.py \
      --run-name "$(basename "$PWD")" \
  > SUMMARY.md
```

Use the CRISP checkout that contains `safety-history`. Do not use `crisp eval` or parse captured stdout/stderr logs.

The renderer creates the totals, validation notes, checkpoint, and table skeleton. Review every `Final edit summary` for concision and technical accuracy. Rewrite awkward session prose, but do not change IDs, counts, tokens, durations, or results without checking the CLI JSON.

Inspect one row without filtering the full JSON externally:

```sh
crisp safety-history --agent-op <agent-op-id>
```

When investigating whether the agent saw an unsafe increase, add
`--include-internal-output`. This exposes the stored output for each detected
agent-side `cargo check-unsafe2` invocation without dumping the entire JSON
session:

```sh
crisp safety-history --agent-op <agent-op-id> --include-internal-output
```

Add `--include-agent-commands` when command ordering is necessary to determine
which intermediate code state an internal check examined. This emits commands,
call IDs, and timestamps, but not unrelated tool outputs.

Use the returned node IDs with `crisp show`, for example `crisp show <agent-op-id>.json_session` or `crisp show <unsafe-check-node-id>`.

## Process a Resumed Run

CRISP appends agent operations to one chronological MVIR history across process restarts. The full agent-operation node ID in `Last processed MVIR agent operation` is the exclusive checkpoint.

1. Read the checkpoint from the existing summary.
2. Confirm it and inspect the number of new rows:

   ```sh
   crisp safety-history --after <full-agent-op-id> --compact | jq '.selection, .checkpoint, .coverage_warnings'
   ```

3. Regenerate the summary from the full history. This recomputes totals and avoids duplicate rows.
4. Confirm that the old checkpoint still appears and the new checkpoint equals the final row's full `agent_op` ID.

Do not infer process boundaries from timestamps or fuel resets. They are unnecessary for incremental bookkeeping.

## Interpret the Fields

- Identify each row by `agent_op`, a SHA-256 MVIR node ID. Use a unique 12-character prefix in the table and the full 64-character ID for the checkpoint.
- Use `after_count` and `delta` for the accepted state. A rejected candidate leaves both unchanged even when its candidate introduced unsafety.
- Use `tokens_used`, which excludes cached input and matches the historical CRISP token metric.
- Use `agent_duration`, the exact Codex task duration in the stored JSON session. Label the total `Total agent runtime`; it excludes CRISP's post-agent validation time.
- Derive rejection prose from `rejection_reason`, `test_exit_code`, and `unsafe_check_diagnostics`.
- Count internal agent experiments only from `internal_check_unsafe2_*`. They are context, not separate summary rows.

Read [references/safety-history-schema.md](references/safety-history-schema.md) when debugging missing fields or writing a custom renderer.

## Verify

Before finishing:

1. Confirm `aggregate.completed_edits` equals the number of table rows.
2. Confirm accepted and rejected totals match the `Result` column.
3. Confirm the final row's unsafe count equals `aggregate.final_unsafe_count`.
4. Inspect every rejected row and preserve its actual CRISP validation cause.
5. Report every `coverage_warnings` entry.
6. Run `crisp safety-history` twice and confirm stable row IDs and aggregates.

The MVIR intentionally cannot report invocations that ended before CRISP stored a `CodexAgentOpNode`; those are outside this summary's row definition. Exact legacy full-step runtime is also unavailable, so never label agent runtime as completed-step runtime.
