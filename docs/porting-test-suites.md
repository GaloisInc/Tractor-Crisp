# Porting Test-Corpus suites to observation tests

Use this guide when migrating an existing `Test-Corpus` suite to the
[standalone test framework](test-framework.md). The worked example is
[`B03_organic/array_list_lib`](../Test-Corpus/Public-Tests/B03_organic/array_list_lib/TESTS.md):
20 JSON vectors and a Cando Rust harness became 20 C observation tests and one
shared helper. The same approach applies to other library suites; executable,
filesystem, and build-configuration cases need the additional steps below.

The migration should preserve one runnable case per existing active vector,
with the same inputs and the same sequence of target API calls. Give cases
recognizable names and keep an explicit mapping when flattening directories or
handling multiple configurations. Preserve the old vectors and runner for
review unless the task explicitly includes removing them.

The new framework records behavior. Build C drivers against original headers
and the initial Rust translation, capture reference observations from that
Rust translation, then reuse the compiled drivers against later Rust versions.
Keep expected observations outside the execution sandbox. Do not embed expected
answers in helpers, include target implementation `.c` files, or link the
original C implementation into the tests.

## Locate the actual test specification

For a library suite, the JSON and `runner/src/main.rs` together define the test.
The symbol named by `harness!` is only part of it: `run(&mut self)` can allocate
objects, call many other functions, install callbacks, mutate state, and return
early. For executable suites, much of the behavior is specified directly by
`argv`, `stdin`, `env`, and directory-vector setup.

These are the useful infrastructure entry points, relative to this repository:

| Source | What to inspect |
| --- | --- |
| [Corpus README](../Test-Corpus/README.md) | Directory conventions, vector formats, configurations, filesystem resources. |
| [Cando `harness!`](../Test-Corpus/tools/cando2/src/lib.rs) | `State`, `ExpectedState`, symbol loading, state comparison. |
| [Cando vector parser](../Test-Corpus/tools/cando2/src/test_vector.rs) | Common JSON fields, cloning input state, `has_ub` handling. |
| [Library runner](../Test-Corpus/tools/cando2/src/runners/lib_runner.rs) | Child-process execution of `State::run`, stream capture, returned state. |
| [Binary runner](../Test-Corpus/tools/cando2/src/runners/bin_runner.rs) | Argument, input, environment, and return-code handling. |
| [Common runner](../Test-Corpus/tools/cando2/src/runners/runner.rs) | Stream defaults, regex matching, UB skipping, fake-time setup. |
| [Vector discovery](../Test-Corpus/tools/test_runner/runtests/test_vector.py) | Which files and directories count as vectors. |
| [Outer setup runner](../Test-Corpus/tools/test_runner/runtests/exec_test_vector/run_cando.py) | Setup environment and working-directory selection. |
| [Filesystem comparison](../Test-Corpus/tools/test_runner/runtests/exec_test_vector/file_diff.py) | File contents, permissions, directories, and deletion manifests. |
| [Old build orchestration](../Test-Corpus/tools/test_runner/runtests/orchestrator/build.py) | Configuration metadata, CMake definitions, Cargo features. |
| [New runner](../scripts/crisp_test_runner.py) | `suite_flags`, `suite_sources`, `discover_tests`, compilation, and runtime staging. |

Read the suite's headers and build configuration as well. Its runner may use
FFI layouts or conventions that the public API explains more clearly. You do
not need to compile Cando or its fuzzing harness just to port fixed vectors.

## Inventory vectors before writing code

Start with `rg --files <case>/test_vectors` and inspect every configuration
directory, not only `default/`. The old discovery code treats each immediate
directory, or each `.json` file other than `configuration.json`, within a build
configuration as a vector. A directory vector contains `cando_vector.json` and
may also have `setup`, `resources/`, and `file_changes.tar.gz`.

Record the configuration, vector path, new test name, input fields, observed
fields, and required fixtures. In particular:

- `configuration.json` is build metadata, not a test. Backup files such as
  `*.json.bak` are not active JSON vectors. Inspect empty files and unusual
  directory entries rather than counting them as ordinary runnable tests.
- The current Cando deserializer treats the **presence** of `has_ub` as true,
  regardless of its JSON value, and the common runner skips such vectors.
  Inventory these separately and document their disposition. Do not silently
  turn old skipped vectors into ordinary reference cases. The new runner has
  no equivalent declarative skip field.
- Preserve separate cases even when their inputs look redundant. Do not merge
  twenty vectors into one process or add newly invented edge cases as part of
  a one-to-one port.
- Keep exact ordering, lengths, duplicate values, zeros, byte strings, and
  numeric widths. Large arrays may be generated from the vectors while making
  the port, but the resulting suite should be self-contained. It should not
  load the old JSON, including expected answers, at execution time.
- `runner/fuzz/` is fuzzing infrastructure, not an additional fixed vector.
  Porting an ongoing fuzzing campaign is a separate task.

If several configurations reuse a vector name, use a documented prefix such
as `config1__test01`. The new runner does not recurse into subdirectories of
`tests/`, so nesting cases under `tests/config1/` does not work.

## Translate the library harness, including its setup

Read `run(&mut self)` in execution order and distinguish JSON inputs from
output placeholders that the harness overwrites. Preserve casts, clamps,
truncation, sentinel values, callback behavior, and early returns. A field
called `returns` is usually an API result stored in the harness state; it is
not necessarily the test process's exit status.

For `array_list_lib`, the old harness does the following:

1. Reset `alloced` and `returns` to zero.
2. Call `arraylist_new(initial_capacity)`. Its apparent `.min(...)` expression
   does not change the value because the input already has type `c_uint`.
3. Append each input entry using `arraylist_append`, in order. Setup can itself
   grow the list, so constructing a final list by hand would change the test.
4. Call `arraylist_insert(list, index, value)` exactly once, including for invalid
   indices. Do not reject bad indices in the driver before the target sees them.
5. Read capacity and the complete resulting data, then free the list through
   `arraylist_free`. Zero length or a NULL data pointer produces an empty vector.

Creation failure returns the original input data with zero capacity and zero
return value. An append failure returns the original input data with the
current capacity and zero return value, after freeing the list. The partially
appended data is not copied back on that path. These details came from the
harness, not from the expected JSON. The new
[helper](../Test-Corpus/Public-Tests/B03_organic/array_list_lib/test_helpers/arraylist_test.c)
preserves them.

Use target allocation, initialization, and destruction APIs for opaque objects.
Do not allocate a guessed Rust-compatible layout or copy private implementation
definitions into the test. Prefer public accessors where the original header
keeps a structure opaque. The array-list header exposes its layout, and the
old harness reads `_alloced`, so that port reads the same field. This does not
establish a layout convention for unrelated libraries. If a required old
observation cannot be reproduced through the original C API, document the
limitation and resolve it explicitly.

For callbacks, port the callback logic and its state too: comparison order,
mapping operations, allocation/free ownership, counters, and destruction side
effects can all be part of the old test. A primary symbol with a simple name
can sit behind a large harness that exercises many APIs.

### Preserve the meaning of pointer values

In the array-list harness, `Vec<usize>` entries are cast directly to `void *`.
They are integer-valued pointer tokens, not addresses of allocated integers.
The C helper therefore casts through `uintptr_t` and prints with `PRIuPTR`.
In particular, zero remains NULL; allocating an integer containing zero would
exercise different behavior. These tokens are never dereferenced by the test.

Other harnesses may actually allocate and dereference pointees. Follow each
harness's ownership and aliasing rules rather than applying the token pattern
everywhere. Preserve ABI types such as `unsigned int`, `size_t`, and fixed-width
integers. For byte buffers and strings, preserve explicit lengths, embedded
NULs, terminators, and encoding decisions made by the old runner.

## Choose observations deliberately

Cando's `ExpectedState` makes every field optional. An omitted or null
`lib_state_out` field is unchecked; it does not mean zero and is not implicitly
copied from `lib_state_in`. The input state is cloned, `run` mutates that clone,
and only supplied expected fields participate in the old state comparison.

In `array_list_lib`, all 20 vectors check data and the insertion return value,
but only the first five specify expected capacity. The new helper emits
`data`, `alloced`, and `returns` for every case because the old harness computes
all three. Recording capacity for every case makes it part of subsequent
reference comparisons, including where old JSON left it unchecked. Document
such choices; if a task requires exactly the old acceptance criteria, restrict
observations to checked fields or provide a deliberate comparison policy.

Print actual target results in a stable, lossless format. JSON works for integer
arrays and scalars; use deterministic escaping for arbitrary strings or a
binary-safe encoding when necessary. Do not print raw allocation addresses,
uninitialized bytes, timings, or unstable iteration order unless they are the
behavior the original test intentionally observes. Preserve meaningful order.
Do not calculate an expected result in the helper and print that instead.

The old comparison semantics have other differences from raw observations:

- Missing `stdout` or `stderr` expectations mean an exact empty string. They
  do not disable stream checking. Exact patterns include all whitespace.
- `is_regex: true` uses Rust regex `is_match`, so matching is not implicitly
  anchored to the whole output. The new runner has no regex comparison layer.
- Scalar floating-point fields receive special approximate comparison in
  `harness!`; see [the implementation](../Test-Corpus/tools/cando2/src/approx_eq.rs),
  which uses `Relative::default().epsilon(1e-6)`. Containers do not automatically
  receive the same treatment. Raw byte comparison of printed floats is a
  different policy; choose any required normalization explicitly.

For simple integer suites, run the new tests against reference Rust and compare
their JSON observations with the fields actually supplied in the old vectors.
This is migration validation outside the suite. A baseline mismatch may reveal
a port error or a translation defect; investigate it instead of changing the
original inputs to obtain a pass.

## Lay out the new suite

The array-list port uses the case directory as `--suite-dir`:

```text
array_list_lib/
  tests.toml
  TESTS.md
  test_helpers/
    arraylist_test.c
    arraylist_test.h
  tests/
    01_insert_front_existing_capacity.c
    ...                              # 20 immediate C files
  test_case/                         # original C/header snapshot
  runner/                            # preserved old harness
  test_vectors/                      # preserved old vectors
  translated_rust/                    # reference Rust, when available
```

Only snapshot `tests.toml`, `tests/`, `test_helpers/`, and any input fixtures
as the executable suite. The co-located original implementation and expected
vectors are not runtime inputs. Add a short suite document describing the
mapping, observation schema, and concrete commands.

The first array-list case is:

```c
// RUN: test_01_insert_front_existing_capacity
#include "arraylist_test.h"

int main(void)
{
    const uintptr_t data[] = {20, 30};
    observe_insert(4, data, sizeof data / sizeof data[0], 0, 10);
    return 0;
}
```

The helper header declares `observe_insert` and includes `<stddef.h>` and
`<stdint.h>`. Empty input cases pass `NULL, 0`; they do not need nonstandard
zero-length C arrays. The helper reports the API return value in stdout, so
an expected insertion rejection still exits the driver normally with zero.

Framework discovery has several consequences for one-to-one ports:

- `tests/foo.c` builds `test_foo`. A single nonempty `// RUN: test_foo` makes
  it a runnable case named `tests/foo.c`. The directive's command is passed
  to Bash with `pipefail`.
- A C file without a directive is still compiled but is not itself run. This
  is useful for a generic driver invoked by several case scripts.
- Each immediate `.sh` or `.py` file is a separate case. Do not add both a
  script and a C `RUN` directive for the same scenario. Script extensions
  select the interpreter even when snapshot permissions lose executable bits.
- All immediate `test_helpers/*.c` files are compiled into one shared library.
  Keep reusable setup/observation code there and give each test executable its
  own `main`. Compilation, like discovery, is not recursive.

### Configure compilation and linking

With `--c-dir <case>/test_case`, array-list needs:

```toml
cflags = ["-Iinclude"]
```

Relative flag paths are resolved from `--c-dir`, not from the suite directory.
The C root, `test_helpers/`, and `tests/` are already include paths. Check
headers and CMake files for additional include directories, preprocessor
definitions, and system libraries. Put ordinary extra link flags in `ldflags`
(for example, `["-lm"]`); existing CRISP `system_libs` entries can also be passed
using repeatable `--system-lib NAME` options. Target Rust libraries are linked
automatically; do not add a path to the original `libdriver.so`.

The new runner never configures or builds the C project. Required generated
headers must already be in the C snapshot. It runs
`cargo build --workspace --message-format=json` in `--rust-dir` and locates
artifacts through Cargo's messages, so do not hardcode `target/debug` paths.
Project library filenames and binary names must be unique, and the names must
stay the same when later Rust snapshots are substituted.
Library targets must produce `cdylib` artifacts and export all C ABI symbols
needed by setup, the operation, and cleanup. Missing symbols are not a reason
to link the original C library or drop setup calls. Check the reference
translation's exports and report deficiencies in that translation separately.

Respect `rust-toolchain.toml`; C2Rust output may require a pinned nightly.
For the array-list validation, the existing translation used
`nightly-2026-08-02`. Work in an isolated Rust copy if necessary to avoid
modifying pre-existing files or mixing generated Cargo output into the port.
Preserve files needed by its actual Cargo build, including build scripts and
local dependencies; the required snapshot varies by project.

## Executable and filesystem vectors

Top-level `stdin`, `env`, `stdout`, and `stderr` fields apply to library vectors
as well as executable vectors. A library driver may need a script or a `RUN`
command with redirection and environment assignments in addition to its C code.

Executable suites often need only one shell or Python script per vector.
Invoke the Rust target by its Cargo binary name on `$PATH`; keep the exact
argument boundaries, environment overrides, stdin bytes, and working-directory
setup. For a simple shell case, a pattern is:

```sh
#!/usr/bin/env bash
set -euo pipefail
export EXAMPLE_MODE=value
mkdir work
cd work
exec driver --flag value < "$TEST_ROOT/fixtures/case01/stdin"
```

Here the fixture must contain the original stdin bytes. Avoid `echo` when it
would add a newline or interpret escapes. `exec` lets the target's exit or
signal reach the runner. If a wrapper must inspect output or files afterward,
preserve stdout, stderr, and the child outcome explicitly; a successful final
`cat` or cleanup command must not hide a target failure. Nonzero exits are
valid observations in the new framework. For binary vectors, the old expected
`rc` defaults to zero; for library vectors, a state field named `returns` is
independent of process status.

Every new case starts in a fresh working directory with private home and temp
directories, empty stdin, locale `C`, timezone `UTC`, and umask `022`. There is
no automatic setup-script invocation, resource copying, or old container path
layout. Use `$TEST_ROOT` to read input fixtures and copy writable resources into
the case workspace. Port any required environment explicitly. The runner
inherits other environment variables; this is not an entirely empty environment.

For directory vectors, inspect the actual `setup` script. The current outer
runner creates `/tmp/runtime` and exports `DEFAULT_RUN_DIR`, `WORKING_DIR_FILE`,
and, when present, `TEST_RESOURCES`. The README calls the first variable
`CANDO_DEFAULT_RUN_DIR`, but the implementation and example scripts use
`DEFAULT_RUN_DIR`. A setup script can write a different working directory into
`WORKING_DIR_FILE`. Translate that behavior into the new case script; those
old variables are not supplied by the new runner. Preserve path meaning when
moving old absolute paths into private working directories.

The old outer runner also checks filesystem changes independently of Cando's
JSON state and streams. Simply porting `cando_vector.json` can therefore lose
the main assertion of a filesystem test. Inspect `file_changes.tar.gz` as an
expected-results artifact, not an input fixture. Reproduce observations of
created/modified file contents, directory presence, permission bits, and
deletions. The old archive's `.cando/deletions.txt` lists deleted absolute
paths, with a trailing slash for directories. Its comparator checks contents
via hashes and permission bits; it does not require matching timestamps.
Use stable relative paths and sorted records in new filesystem observations,
and retain checks for unexpected changes within the tested scope. Observe files
before the private workspace disappears. Do not ship the expected archive into
the runtime suite.

The old recording scope includes setup as well as the target invocation, so
inspect setup effects alongside the expected archive. The old filesystem
comparator also deliberately ignores `.git/index` contents while retaining
path and permission information for it; preserve such exclusions where relevant.

Also inspect special environment behavior. Cando notices `FAKETIME` in a vector
and injects `LD_PRELOAD` using `LIBFAKETIME_SO`. The new runner removes inherited
`LD_PRELOAD` and `LD_AUDIT` and provides no automatic fake-time setup. Merely
copying `FAKETIME` does not preserve that behavior. Recreate any required
controlled environment in the test's explicit launch/setup and record the
runtime dependency, or document the unresolved limitation.

## Preserve build configurations separately

The old runner reads `test_case/configuration.json` for allowed values and
`test_vectors/<configuration>/configuration.json` for selected values. The
implementation expects these under `configurable_variables`. It derives CMake
definitions and Cargo features; inspect both the metadata and the project's
build scripts rather than inferring a configuration from its directory name.
Check the actual feature mapping if a baseline disagrees with its vectors:
the current `_fmt_cargo_features` emits a feature name for a boolean entry even
when its value is false.

The new runner does not consume that metadata or offer a Cargo `--features`
option: it builds the supplied workspace with its configured defaults. A
`tests.toml` preprocessor definition affects the C drivers, not Cargo features.
Use separately prepared, configuration-correct Rust/C snapshots and suitable
suite subsets or suite roots, with separate compiled bundles and references.
Keep artifact names stable between the reference and candidate for each
configuration. Document how each old vector maps to its configured run; do
not run all configurations against the default library and claim full parity.

## Validate the port

First compare both names and exact inputs. Counts alone do not catch a missing
case replaced by a duplicate. Verify every scalar and every array element,
including the array-list large inputs of lengths 256, 512, and 768. Confirm
setup calls, callbacks, and failure paths by reviewing the helper against the
old `run` function. Then use the new runner itself for build and discovery.

For array-list, from the repository root:

```sh
case_dir=Test-Corpus/Public-Tests/B03_organic/array_list_lib
port_work=$(mktemp -d /tmp/array-list-port.XXXXXX)
reference_rust="$case_dir/translated_rust"

python3 scripts/crisp_test_runner.py build \
  --suite-dir "$case_dir" --c-dir "$case_dir/test_case" \
  --rust-dir "$reference_rust" --build-dir "$port_work/compiled"

python3 scripts/crisp_test_runner.py run \
  --suite-dir "$case_dir" --rust-dir "$reference_rust" \
  --build-dir "$port_work/compiled" --output-dir "$port_work/reference"
```

These commands require an available reference Rust translation. If it is
missing, report that runtime validation is unavailable rather than substituting
the original C target as the new framework's reference. The array-list port was
validated against its existing translation with all 20 cases matching old
expected data/return fields and explicit capacity expectations. All 21 C source
files (20 drivers and the helper) also passed strict compiler checks:

```sh
clang -std=c11 -Wall -Wextra -Werror -fsyntax-only \
  -I "$case_dir/test_case/include" -I "$case_dir/test_helpers" \
  "$case_dir"/test_helpers/*.c "$case_dir"/tests/*.c
```

For this simple all-default suite, the following verifies actual discovery and
the old expected fields against observations. For other suites, adapt the
mapping for scripts, configurations, skipped vectors, and comparison semantics.
Run this validation outside the execution sandbox:

```sh
python3 - "$case_dir" "$port_work/reference" <<'PY'
import importlib.util
import json
from pathlib import Path
import sys

suite, observed = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location("runner", "scripts/crisp_test_runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

vectors = {f"tests/{p.stem}.c": json.loads(p.read_text())
           for p in (suite / "test_vectors/default").glob("*.json")}
discovered = [name for name, _ in runner.discover_tests(suite)]
assert len(discovered) == len(vectors) and set(discovered) == set(vectors)
summary = json.loads((observed / "results.json").read_text())
assert summary["complete"]
results = summary["tests"]
assert len(results) == len(vectors)
assert {r["name"] for r in results} == set(vectors)
for result in results:
    assert result["status"] == "exited" and result["returncode"] == 0, result
    assert (observed / result["stderr"]).read_bytes() == b"", result["name"]
    actual = json.loads((observed / result["stdout"]).read_bytes())
    expected = vectors[result["name"]]["lib_state_out"]
    for field, value in expected.items():
        if value is not None:
            assert actual[field] == value, (result["name"], field, actual[field], value)
print(f"Validated {len(results)} cases")
PY
```

This checks case mapping and observations, not input-source parity; inspect or
independently parse the C inputs as a separate check. For fields emitted but
not explicitly expected by old vectors, compare with the old harness's actual
state when needed. Do not fill in guessed expected fields.

For subsequent candidate runs, reuse the entire compiled directory (`bin/`,
optional `lib/`, and `manifest.json`) and change only the Rust snapshot and
observation destination:

```sh
candidate_rust=/path/to/candidate-rust
python3 scripts/crisp_test_runner.py run \
  --suite-dir "$case_dir" --rust-dir "$candidate_rust" \
  --build-dir "$port_work/compiled" --output-dir "$port_work/candidate"
```

Compare cases by name, return code, and the exact bytes of stdout and stderr;
ignore elapsed time. The runner performs no comparison. `complete: true` and
runner exit zero only mean every case exited normally, including cases with
nonzero exit codes. Signals, timeouts, output limits, launch failures, and
unrun cases need investigation even if their partial output looks plausible.
Output directories must be new or empty. Repeated runs in different output
directories can reveal unstable observations.

Keep suite sources and their compiled bundle paired: the manifest does not
validate source fingerprints. Rebuild after editing C tests or helpers. The
run operation builds candidate Rust but never recompiles or relinks the test
drivers, and stages only that run's Rust libraries. This is what makes the
same C ABI tests useful across Rust refactors. For optional sanitizer checks,
build a separate bundle with `--sanitize`; that instruments C drivers/helpers,
not the Rust target, and wrappers must retain diagnostics and child failures.

For MVIR integration, the test tree is rooted at the suite files, not at the
corpus case's parent directory. Use `compile_tests` and
`run_observation_tests` with the paths described in the
[framework guide](test-framework.md#mvir-analysis-passes). Include fixtures in
that tree and keep references outside it. The standalone runner enforces
deadlines, output limits, and descendant cleanup; the caller supplies
filesystem/network isolation and other sandbox limits.

Finally, inspect changes in both repositories: `Test-Corpus` is a Git submodule,
so use `git -C Test-Corpus status --short` as well as the outer repository's
status. Keep build artifacts and generated validation observations out of the
port, and preserve unrelated existing work. Report the case mapping/count,
observation choices, commands actually verified, and any remaining limitations.
