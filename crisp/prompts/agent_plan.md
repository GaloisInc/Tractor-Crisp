Write `SAFETY_PLAN.md`: a migration plan for refactoring the Rust code in `{cargo_dir_path}` to be fully safe without changing its behavior.  The goal state uses safe memory management (`Box`/`Vec`/`Rc`) and safe pointer types (`&T`/`&[T]`) throughout, with all remaining unsafety confined to FFI entry points.

This is a planning-only step.  Do not modify, create, rename, or delete any source files; your only output is `SAFETY_PLAN.md` at the repository root.

# How the plan will be used

The plan is executed incrementally by fresh agent instances that do not see this conversation.  Each instance reads `SAFETY_PLAN.md`, gets a small work budget, and is usually assigned one specific target -- a single struct field or a single function, chosen by an external harness from the unsafe-code inventory in `$FIND_UNSAFE2_JSON_DIR` -- though sometimes it is simply told to continue the plan.  Each instance updates the Status section before finishing.  The harness independently validates every step (build, tests, and an unsafe-code check) and injects the exact validation commands into every instance's instructions.

Consequences for how you must write the plan:
- It is a lookup document, not a narrative.  An agent assigned an arbitrary struct field or function must be able to find the cluster covering its target and read what to do.  Key every entry by the exact symbol names used in the inventory JSON, so targets can be found by grep.
- Every line is re-read on every step.  Keep the plan compact: prefer tables over prose, don't restate what the code already shows, and aim for well under 400 lines even for large codebases.
- Do not include any build, test, lint, or verification commands, and no command-based acceptance gates; the harness supplies all validation.  State done-criteria in semantic terms tied to the inventory, e.g. "no field of struct X is a raw pointer" or "no function in this cluster is an `unsafe fn`".
- Do not spell out full speculative Rust signatures; name the target types and ownership model and let the executing agent work out details against the code it sees.

# Analysis phase

Use the following read-only custom sub-agents, one per independent analysis lens:
- `ffi_abi_analyst`: exported functions and data, ABI compatibility constraints.
- `ownership_analyst`: allocation, ownership, aliasing, lifetimes, `static mut` globals, and unions.
- `collections_analyst`: custom containers, plus internal function-pointer tables and dispatch.
- `strings_analyst`: strings, byte buffers, text, and paths.
- `libc_analyst`: C, POSIX, and platform library calls.
- `macro_analyst`: oversized or repetitive translated functions caused by C macro expansion.

Spawn exactly one fresh instance of each named agent with `fork_turns="none"`.  Each agent already carries its lens instructions and the shared constraints (`.codex/safety_constraints.md`); keep spawn messages short, containing only the run-specific facts:
- the Rust project path: `{cargo_dir_path}`;
- the location of the unsafe-code inventory: the concrete directory path from `$FIND_UNSAFE2_JSON_DIR`; and
- whether the original C sources are present in the workspace, and where.

Wait for all agents to finish.  If an agent fails or returns an unusably thin report, note the gap and proceed with the remaining reports.  Reconcile overlaps, dependencies, and disagreements between the reports rather than concatenating them.  Cross-check coverage against the inventory: every file with unsafe findings must be covered by exactly one cluster in the plan.  Then have only the parent agent write `SAFETY_PLAN.md`; do not delegate the writing.

# Required plan structure

`SAFETY_PLAN.md` must contain exactly these four sections:

1. `## FFI entry point rules` -- the rules block below, copied verbatim and marked as immutable.
2. `## Conventions` -- the crate-wide decisions every step must follow: standard type mappings (e.g. which pointer-plus-length idioms become `&[u8]`, when owned text is `String` vs `Vec<u8>`), the error-handling model, callback handling, and the standard moves: raw pointers -> references or smart pointers; `static mut` -> immutable `static`/`const`, `OnceLock`, or atomics; unions -> enums or explicit safe fields; unsafe FFI imports -> safe std equivalents (e.g. `printf` -> `println!`); custom containers -> std collections where semantics allow; remove `unsafe` qualifiers from functions that no longer need them.  Also state the dependency policy: a Rust crate may replace a dependency of the original C project (with one line of justification), but must never replace the project under migration itself, in whole or in module-sized parts.
3. `## Cluster guide` -- the codebase partitioned into dependency-ordered clusters (a module or data-structure family plus the functions around it).  For each cluster: the files it covers; prerequisites (clusters or conventions that must land first); a table mapping each struct field that contains raw pointers to its recommended safe type with a one-line rationale; the key functions and how they change; pitfalls and risky edge cases; and semantic done-criteria.  Group same-shaped migrations into a single unit: if migrating `f` to `String` implies the same change in `g`, plan them together.
4. `## Status` -- the working log.  Initialize it with "no work started" plus the recommended first units; executing instances append what they did, what is in flight, and dead ends to avoid.

# FFI entry point rules (copy verbatim into the plan)

{ffi_entry_point_rules}
