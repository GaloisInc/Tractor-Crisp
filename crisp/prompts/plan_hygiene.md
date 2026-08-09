`SAFETY_PLAN.md` guides an incremental migration of the Rust project in `{cargo_dir_path}` toward safety.  It has accumulated entries across many iterations.  This is a maintenance-only step: rewrite the plan into a compact, accurate guide for the next iterations.

- Collapse completed work into a one-line summary each; delete step-by-step history that no longer guides future edits.
- Merge repeated or overlapping dead-end and blocked entries into one precise note each.  Keep each dead end's citation of the gate that rejected it (the checker diagnostic, review finding, or failing test).  Mark dead ends that lack a citation as "(uncited)" instead of deleting them.  A merged note covers only the functions and types the originals named; keep those names and do not widen its scope.
- Sharpen vague notes: a future iteration must be able to tell exactly what was tried and why it failed.
- Keep the FFI entry point rules section verbatim.
- Order remaining-work items by the unsafe-operation counts below, largest first.  Do not add new analysis or new work items, and do not change the content of what remains to be done.

Where the remaining unsafe operations are (implementation code only; FFI entry points are exempt):

{unsafe_inventory}

Do not modify, create, rename, or delete any source files; edit only `SAFETY_PLAN.md`.
