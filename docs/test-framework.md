# Standalone observation-test runner

`scripts/crisp_test_runner.py` builds C tests against original headers and runs
them against Rust libraries and executables. It requires Linux, Python 3.11+,
Cargo, Clang, and Bash. It uses only Python's standard library and does not
import CRISP. The `tractor-crisp-user` image installs it at
`/usr/local/bin/crisp_test_runner.py`.

For migrating existing corpus tests, see [Porting Test-Corpus suites](porting-test-suites.md).

The caller provides the sandbox and input snapshots. The script does not create
a filesystem/network sandbox, compare results, or generate tests. It does not
build or execute the original C implementation. Capture reference observations
against the initial C2Rust output, then run the same compiled suite against
refactored Rust. Keep expected observations outside the execution sandbox.

## Suite layout

```text
suite/
  tests.toml               # optional compilation/link flags
  test_helpers/
    helpers.c              # all *.c compiled into one shared helper library
    helpers.h
  tests/
    simple.c               # one executable per *.c
    complex.c
    complex.sh             # invokes test_complex with custom arguments/setup
    cli.py                 # can invoke target binaries directly
  fixtures/                # optional resources, read using $TEST_ROOT
```

`tests.toml` belongs to the test suite and may be edited by the generator:

```toml
cflags = ["-Iinclude", "-D_FILE_OFFSET_BITS=64"]
ldflags = ["-lm"]
```

Both fields default to empty arrays. Flags apply to helpers and individual
tests. Relative flag paths are interpreted from `--c-dir`, the original C
project root. That root, `test_helpers/`, and `tests/` are automatically added
to the header search path. Required generated headers must already be present
in the supplied C snapshot. No C configure/build commands are run.

Each `tests/foo.c` becomes `test_foo`, available through `$PATH`. Helpers become
`libcrisp_test_helpers.so`; tests link against it and all project Rust shared
libraries. The helper directory can be absent. A suite containing only scripts
needs no C programs or helpers.

A simple test can be entirely in one file:

```c
// RUN: test_simple
#include <stdio.h>
#include <example.h>

int main(void) {
    printf("result=%d\n", example_function(42));
    return 0;
}
```

Discovery examines only immediate files in `tests/`. It runs each `.sh` through
`bash -o pipefail`, each `.py` through `python3`, and other executable non-C
files directly. Shell/Python scripts work even when snapshots have lost their
executable permissions. Each C file with a single nonempty `// RUN:` directive
adds another test. Its command is passed to `bash -o pipefail -c`. C files without
a directive are compiled but run only when invoked by a script or another test.
A script and a `RUN` directive are independent tests; use just one if they would
duplicate the same scenario. Files are discovered in sorted filename order.

Each test starts in a new empty temporary working directory with private home
and temporary directories, empty stdin, locale `C`, timezone `UTC`, and umask
`022`. Redirection and setup belong in the script or `RUN` command. Test programs
may create files, start local services, run subprocesses, and normalize their
observations. Any needed cleanup in a script should propagate failures; the
runner also kills and reaps subprocesses, including detached descendants, when
the top-level command finishes or reaches a limit.

The runner sets:

- `$PATH`: compiled `test_*` programs, then Rust target binaries, then the
  inherited tool/interpreter search path.
- `$LD_LIBRARY_PATH`: the staged helper library and current Rust libraries.
  The inherited library path, `LD_PRELOAD`, and `LD_AUDIT` are discarded.
- `$TEST_ROOT`: absolute suite root, for reading fixtures and scripts.
- `$TEST_BINS`: absolute directory containing the compiled `test_*` programs.
- `$HOME` and `$TMPDIR`: private directories for the current test.

Tests use library-provided initialization, allocation, and destruction APIs
for opaque objects. They may inspect caller-visible layouts according to the
original C API. Neither the test sources nor helpers should include original
implementation `.c` files or link an original target implementation.

## Build test binaries

```sh
python3 scripts/crisp_test_runner.py build \
  --suite-dir /work/suite \
  --c-dir /work/original-c \
  --rust-dir /work/reference-rust \
  --build-dir /work/compiled-tests
```

`--rust-dir` is the directory containing the Cargo workspace's `Cargo.toml`.
The runner executes `cargo build --workspace --message-format=json` there,
uses Cargo's reported artifact paths, and builds helpers/tests against the
produced `cdylib` libraries. It ignores dependency artifacts outside the supplied
Rust directory. Project binary names and library filenames must be unique.
All target names must stay the same in subsequent Rust runs.

The output directory contains `bin/test_*`, an optional `lib/` helper library,
and `manifest.json`. Preserve the entire directory as the compiled suite.
Ordinary executable permissions are restored when staging a run, so copying
these files through MVIR's byte-only file storage is supported.

The script always compiles when invoked; CRISP caches the operation using MVIR
input nodes. The manifest records only bundle contents and instrumentation mode,
with no fingerprints, header-dependency cache, or source consistency checks.
Cargo handles its own build cache. A failed compilation does not publish a new
manifest or replace existing executables. Use a separate build directory for
each concurrently built suite; concurrent writes to one build directory are
not supported.

Optional build arguments:

- `--system-lib NAME`, repeatable: pass existing CRISP artifact `system_libs`
  entries here when test infrastructure needs them. Target libraries themselves
  are linked automatically.
- `--cc clang`: override the compiler executable.
- `--build-timeout 300`: seconds allowed for each Cargo/compiler/link command.
- `--sanitize`: instrument C tests and helpers with ASan and UBSan. This does
  **not** instrument the Rust target; a Rust sanitizer build requires separate
  toolchain/build configuration. Use a separate compiled suite for this variant.
  Reports remain in captured stderr; external symbolization is disabled to avoid
  blocking inside the sandbox, while built-in UBSan source locations and raw
  stack addresses remain available. Scripts must propagate child sanitizer
  failures and retain their diagnostics if a higher layer is validating UB.

The `build` command writes a JSON summary to stdout. Build commands and
diagnostics go to stderr.

## Run against Rust

```sh
python3 scripts/crisp_test_runner.py run \
  --suite-dir /work/suite \
  --rust-dir /work/candidate-rust \
  --build-dir /work/compiled-tests \
  --output-dir /work/candidate-observations \
  --test-timeout 5 \
  --suite-timeout 30
```

The run operation requires no original C snapshot or CRISP installation. It
builds Rust using Cargo and stages only its runtime artifacts plus
the already compiled test executables/helper library. It never recompiles or
relinks those executables. Staging uses library basenames and the selected
Rust artifacts, so drivers have no automatic fallback to the Rust libraries
used during test compilation.

The output directory must be new or empty. Each test gets files named after its
full suite-relative filename, including the source extension:

```text
observations/
  results.json
  tests/
    simple.c.stdout
    simple.c.stderr
    simple.c.json
    complex.sh.stdout
    complex.sh.stderr
    complex.sh.json
```

The `.stdout` and `.stderr` files hold raw bytes. Each test's `.json` file
records its name, termination status, return code, and any failure reason:

```json
{
  "name": "tests/simple.c",
  "returncode": 0,
  "status": "exited"
}
```

`results.json` also indexes all tests and their files:

```json
{
  "version": 1,
  "complete": true,
  "tests": [
    {
      "name": "tests/simple.c",
      "status": "exited",
      "returncode": 0,
      "stdout": "tests/simple.c.stdout",
      "stderr": "tests/simple.c.stderr",
      "metadata": "tests/simple.c.json"
    }
  ]
}
```

Stdout and stderr are kept separate and may contain arbitrary bytes, including
NULs and invalid UTF-8. Normal nonzero exits are observations: they do not make
the runner command fail. `complete` means every test exited normally, not that
its observations matched a reference. All files in the output directory are
intended for comparison, so matching behavior produces byte-identical snapshots:

```sh
diff -Nurd golden-outputs/ current-outputs/
```

Metadata detects exit-code changes even when both streams are empty. Retaining
source extensions distinguishes, for example, `tests/foo.c` and `tests/foo.sh`.
Adding a test does not rename existing tests' files. Elapsed time is included
only in the JSON summary printed to the command's stdout, not in the output
directory. The script performs no implicit stream normalization or reference
updating.

Other statuses are `signaled`, `timeout`, `output_limit`, `launch_error`, and
`not_run`. Negative return codes identify signals. A suite deadline marks
remaining cases `not_run` with reason `suite_timeout`. Each stream is limited to
1 MiB by default (`--output-limit BYTES`); an overflow records the bounded prefix
and an error status, so it cannot be treated as a successful observation.

The suite deadline covers case execution and cleanup; it excludes the preceding
Cargo build and runtime staging. `--build-timeout` controls that Cargo build
separately. A case failure does not prevent later cases from being observed
while the suite still has time remaining. Repetitions are ordinary additional
`run` invocations with different output directories. The script never caches
executions; the MVIR pass uses an explicit trial number to distinguish repeated
executions from cache lookups.

Command exit status is zero after a successful build or a complete run, and
one for configuration/build errors or any abnormal/incomplete run. Build errors
go to stderr; completed run results are also printed as JSON to stdout.

The caller should use an immutable suite and an isolated sandbox for each
operation, with no original target implementations or reference observations
accessible to candidate tests. Filesystem/network isolation and memory/disk/
process-count limits belong to that sandbox. The script enforces execution
deadlines, stream-size limits, and subprocess cleanup itself.

## MVIR analysis passes

`crisp.analysis` exposes two cached passes:

```python
compile_tests(cfg, mvir, c_code, test_code, rust_code, cmd)
run_observation_tests(cfg, mvir, rust_code, test_code, test_binaries, cmd, trial=0)
```

The source and binary inputs are `TreeNode`s. `cmd` is the actual standalone
runner command, including paths and options, and is part of the cache key.
Compilation also needs the initial Rust tree because the C drivers must link
against its shared libraries. Later runs reuse the resulting binary tree.
Tool versions, the runner implementation, and Docker image identity are not
tracked separately. As with other analysis passes, `cfg` selects the sandbox
but does not participate in cache matching.

Each pass checks out its inputs under fixed sandbox-relative directories:

| Input/output | Sandbox directory |
| --- | --- |
| Original C snapshot (compile only) | `c_code/` |
| Rust snapshot | `rust_code/` |
| Suite snapshot, rooted at `tests.toml`, `tests/`, `test_helpers/` | `test_code/` |
| Compiled suite | `test_binaries/` |
| Run observations | `test_results/` |

For example, when the Rust tree contains `rust/Cargo.toml`, use:

```python
build_cmd = [
    "crisp_test_runner.py", "build",
    "--c-dir", "c_code", "--suite-dir", "test_code",
    "--rust-dir", "rust_code/rust", "--build-dir", "test_binaries",
]
built = compile_tests(cfg, mvir, c_code, test_code, initial_rust, build_cmd)
# Check built.exit_code before accessing its binary tree.
binaries = mvir.node(built.test_binaries)

run_cmd = [
    "crisp_test_runner.py", "run",
    "--suite-dir", "test_code", "--rust-dir", "rust_code/rust",
    "--build-dir", "test_binaries", "--output-dir", "test_results",
]
observed = run_observation_tests(cfg, mvir, candidate_rust, test_code, binaries, run_cmd)
```

The compile result stores `exit_code`, build logs, and an optional
`test_binaries` tree rooted at `bin/`, `lib/`, and `manifest.json`. The run result
stores logs, `exit_code`, and an optional `outputs` tree rooted at `results.json`
and the raw stream files. Partial observations are retained on runtime failures;
build failures that produce no observations have no output tree.

Identical inputs, commands, and trial numbers reuse the existing MVIR result.
For stability checks, call with `trial=0`, `trial=1`, etc.; each distinct trial
executes in a fresh sandbox. The caller must select matching immutable suite
sources and compiled binaries. No separate input fingerprinting is needed.
