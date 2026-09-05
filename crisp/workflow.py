import cbor
import dataclasses
from dataclasses import dataclass
from datetime import datetime
import functools
import inspect
import os
from pathlib import Path
import re
import subprocess
import sys
import toml
import typing
from typing import Any, Callable

from . import agent, analysis, llm
from .analysis import COMPILE_COMMANDS_PATH
from .config import Config
from .error import CrispError
from .mvir import (
    MVIR, Node, FileNode, TreeNode, CompileCommandsOpNode, TranspileOpNode,
    LlmOpNode, CodexReviewOpNode, TestResultNode, FindUnsafeAnalysisNode, SplitFfiOpNode,
    CargoCheckJsonAnalysisNode, EditOpNode, WorkflowStepInputsNode,
    WorkflowStepNode, SplitOpNode, MergeOpNode, CrateNode, DefNode,
    RelatedDeclsOpNode, FindUnsafe2AnalysisNode, CheckUnsafe2AnalysisNode,
    CargoFixOpNode,
)
from .sandbox import run_sandbox
from .work_dir import lock_work_dir

LLM_SAFETY_PROMPT = '''
This Rust code was auto-translated from C, so it is partly unsafe. Your task is to convert it to safe Rust, without changing its behavior. You must replace all unsafe operations (such as raw pointer dereferences and libc calls) with safe ones, so that you can remove unsafe blocks from the code and convert unsafe functions to safe ones. You may adjust types and data structures (such as replacing raw pointers with safe references) as needed to accomplish this.

HOWEVER, any function marked #[no_mangle] or #[export_name] is an FFI entry point, which means its signature must not be changed. If such a function has unsafe types (such as raw pointers) in its signature, you must leave them unmodified. You may still update the function body if needed to account for changes elsewhere in the code.

After making the code safe, {output_instructions_lowercase}

{input_files}
'''

LLM_SAFETY_PROMPT_NO_FFI = '''
This Rust code was auto-translated from C, so it is partly unsafe. Your task is to convert it to safe Rust, without changing its behavior. You must replace all unsafe operations (such as raw pointer dereferences and libc calls) with safe ones, so that you can remove unsafe blocks from the code and convert unsafe functions to safe ones. You may adjust types and data structures (such as replacing raw pointers with safe references) as needed to accomplish this.

After making the code safe, {output_instructions_lowercase}

{input_files}
'''

LLM_REPAIR_PROMPT = '''
I tried compiling this Rust code and running the tests, but I got an error. Please fix the error so the code compiles and passes the tests. Try to avoid introducing any more unsafe code beyond what's already there.

{output_instructions}

{input_files}

Build/test logs:

```
{test_output}
```
'''

LLM_REPAIR_COMPILE_PROMPT = '''
I tried compiling this Rust code, but I got an error. Please fix the error so the code compiles.

Don't add new unsafe blocks unless absolutely necessary. If the error is due to an unsafe function call or other operation, try to replace it with an equivalent safe operation instead.

{output_instructions}

{input_files}

Compiler logs:

```
{stderr}
```
'''

LLM_REPAIR_SAFETY_PROMPT = '''
A recent change to this Rust code added new unsafety that wasn't present
before.  See the report below and fix the indicated sources of unsafety.  Avoid
adding new unsafe code, as it will be rejected by the same check.

{output_instructions}

{input_files}

Unsafety report:

```
{logs}
```
'''

LLM_PROMPT_REPAIR_CALL_SITES = '''
The file `ffi.rs` below contains FFI wrapper functions, which expose various Rust functions to C. The signatures of the underlying Rust functions have changed; please update the wrappers to match.

{output_instructions}

Old signatures:
```rust
{old_sigs}
```

New signatures:
```rust
{new_sigs}
```

{input_files}
'''

# Prompt templates live in `crisp/prompts/`, one file per prompt.  They are
# `str.format` templates; `{ffi_entry_point_rules}` splices the shared FFI
# rules block into the prompts that use it.
_PROMPT_DIR = Path(__file__).parent / 'prompts'

def _prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text()

FFI_ENTRY_POINT_RULES = _prompt('ffi_entry_point_rules.md').strip()

AGENT_PLAN_PROMPT = _prompt('agent_plan.md')

AGENT_FFI_REVIEW_PROMPT = _prompt('ffi_review.md')

# Prose statement of the gates `check-unsafe2` enforces; update it
# alongside any checker gate change.
CHECKER_RULES = _prompt('checker_rules.md').strip()

TOLERATED_UNSAFETY_RULES = _prompt('tolerated_unsafety_rules.md').strip()

AGENT_TOLERATED_REVIEW_PROMPT = _prompt('tolerated_review.md')

# `codex exec review` renders each finding as `- [P1] title — file:line`;
# a clean review is prose with no such lines.
AGENT_FFI_REVIEW_FINDING_RE = re.compile(r'^\s*-\s*\[P\d+\]', re.MULTILINE)
# Same format, capturing the title for `merge_ffi_finding_titles`.
AGENT_FFI_REVIEW_FINDING_TITLE_RE = re.compile(
    r'^\s*-\s*\[P\d+\]\s*(.+?)\s*$', re.MULTILINE)
# Trailing `— file:line` location; stripped because locations go stale.
AGENT_FFI_REVIEW_FINDING_LOCATION_RE = re.compile(
    r'\s+(?:—|--)\s+\S+:\d+(?:[-:]\d+)*$')

FFI_SEEN_FINDINGS_CAP = 10

def merge_ffi_finding_titles(seen: list[str], report: str) -> list[str]:
    """
    Merge finding titles from a rejecting FFI review `report` into `seen`,
    deduplicated and bounded to the most recent `FFI_SEEN_FINDINGS_CAP`.
    """
    for m in AGENT_FFI_REVIEW_FINDING_TITLE_RE.finditer(report):
        title = AGENT_FFI_REVIEW_FINDING_LOCATION_RE.sub('', m.group(1)).strip()
        if title and title not in seen:
            seen.append(title)
    return seen[-FFI_SEEN_FINDINGS_CAP:]

AGENT_SAFETY_PROMPT = '''
Consult `SAFETY_PLAN.md` for the crate-wide conventions and the cluster covering your target.  It is read-only reference: notes in it are guidance, not rules — only the rules stated below bind your work.

{menu}

{target_goal}

{after_refactoring_instruction}

Check your work using this command:
```sh
cargo check-unsafe2 --manifest-path {cargo_dir_path}/Cargo.toml
```
{checker_rules}

Checker-tolerated warnings are judged under these binding rules:

{tolerated_unsafety_rules}

FFI entry points are judged under these binding rules:

{ffi_entry_point_rules}

Begin your final message with a line `TARGET: <name>` naming the function (or `Type.field`) this step worked on, and end it with one of these lines — or no verdict line, which means DONE: the step's work is complete and is judged against the code as it stood when the step began.
- `CONTINUE: <what the next invocation must finish>` — the change is sound but needs more than one invocation (an ownership facade, a state-machine conversion). The step then spans several invocations, judged once as a whole. The unsafe baseline in your workspace stays pinned to the step's start, so `cargo check-unsafe2` always shows the exact judgment the whole step will face; failing it mid-step is expected, and it must pass before you declare DONE. Every invocation must still build — one that does not is discarded, and the step resumes from the last good state.
- `BLOCKED: <target> — <verbatim gate diagnostic or one-line reason>` — no legal reduction exists at that target; your code edits will be discarded and the target deferred until another target lands an unsafe-count reduction. Do not use BLOCKED for work that is merely too large for one invocation — that is what CONTINUE is for.
'''

def _abi_header_type(name: str) -> bool:
    # C-header (`*_h` module) types carry ABI layout; their fields cannot go.
    return any(seg.endswith('_h') for seg in name.split('::')[1:-1])

def menu_targets(
    fns: dict, types: dict, suppressed: 'frozenset[str] | set[str]' = frozenset(),
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """
    The live target lists from merged inventory records: counted non-FFI
    functions by unsafe operations, and `Type.field` entries by raw-pointer
    count, both largest first and minus `suppressed`.  Fields name the
    ownership unit a redesign must change, so they are offered alongside
    functions rather than hidden behind them.
    """
    fn_list = sorted(
        ((name, r.get('total_unsafe', 0)) for name, r in fns.items()
            if r.get('ffi_symbol') is None and not r.get('is_ffi_entry_point')
                and r.get('total_unsafe', 0) > 0
                and name not in suppressed),
        key = lambda kv: (-kv[1], kv[0]))
    field_list = sorted(
        ((f'{ty}.{field}', n) for ty, r in types.items()
            for field, n in r.get('field_contains_raw_ptr', {}).items()
            # A type alias records its target under the pseudo-field "type".
            if n > 0 and field != 'type'
                and not _abi_header_type(ty)
                and f'{ty}.{field}' not in suppressed),
        key = lambda kv: (-kv[1], kv[0]))
    return fn_list, field_list

# The menu is the target vocabulary: suppression rotates lower-ranked
# targets into view.  Per-file totals give the mass map in a line per file.
MENU_MAX_FNS = 20
MENU_MAX_FIELDS = 10

def format_menu(
    fns: dict, types: dict, suppressed: 'frozenset[str] | set[str]' = frozenset(),
) -> str:
    """Render the target menu for the step prompt, biggest targets first."""
    fn_list, field_list = menu_targets(fns, types)
    live_names = {name for name, _ in fn_list + field_list}
    live_suppressed = set(suppressed) & live_names
    fn_list = [(name, mass) for name, mass in fn_list
        if name not in live_suppressed]
    field_list = [(name, mass) for name, mass in field_list
        if name not in live_suppressed]
    by_file = {}
    for name, r in fns.items():
        if (r.get('ffi_symbol') is None and not r.get('is_ffi_entry_point')
                and r.get('total_unsafe', 0) > 0):
            f = r.get('filename', '')
            by_file[f] = by_file.get(f, 0) + r['total_unsafe']
    lines = ['Where the remaining unsafe operations are — pick one target '
        'from the lists below and name it in your `TARGET:` line.']
    lines.append('')
    lines.append('By file:')
    lines.extend(f'- {f}: {n}'
        for f, n in sorted(by_file.items(), key = lambda kv: (-kv[1], kv[0])))
    lines.append('')
    lines.append('Largest open functions:')
    lines.extend(f'- {name}: {n}' for name, n in fn_list[:MENU_MAX_FNS])
    if field_list:
        lines.append('')
        lines.append('Fields still carrying raw pointers '
            '(name as `Type.field`):')
        lines.extend(f'- {name}: {n}' for name, n in field_list[:MENU_MAX_FIELDS])
    if live_suppressed:
        lines.append('')
        lines.append('Deferred until another target reduces unsafe '
            '— do not target: '
            + ', '.join(f'`{name}`' for name in sorted(live_suppressed)))
    return '\n'.join(lines)


class StepOutcome(typing.NamedTuple):
    """
    Result of one judged safety step.  `code` is the accepted tree, the
    unchanged input tree for a blocked step, or `None` when a gate or
    review rejected the step.

    A tuple, so the step logger prints indexed `result[N]` lines; log
    tooling keys on `result[0]` and `result[2]`, with new fields appended.
    """
    code: 'TreeNode | None'
    plans: 'TreeNode | None'
    ffi_report: str | None
    target: str | None
    note: str
    invocations: int


AGENT_FINAL_INVOCATION_NOTE = (
    'This is the final invocation of this step: finish the work and shed any '
    'transitional scaffolding, then declare DONE — or declare BLOCKED. '
    'A CONTINUE here is judged as it stands.')

AGENT_CONTINUE_HANDOFF = (
    'This step continues from your previous invocation, which reported: {note}')

# Warning lines printed by `check-unsafe2` for tolerated changes.  Anchored on
# the checker's distinctive suffixes so rustc/cargo warnings don't match.
CHECK_UNSAFE2_WARNING_RES = [
    re.compile(r'^warning: .+ increased: \d+ -> \d+$'),
    re.compile(r'^warning: .+ changed: false -> true$'),
    re.compile(r'^warning: .+ entry points must stay thin$'),
]

def extract_checker_warnings(logs: str) -> list[str]:
    """
    Extract `check-unsafe2` tolerated-change warnings from a check run's
    output.
    """
    return [
        line for line in logs.splitlines()
        if any(r.match(line) for r in CHECK_UNSAFE2_WARNING_RES)
    ]

AGENT_FFI_REJECTED_PROMPT = '''
A previous attempt at `{target}` was rejected by review. The reviewer reported:

{report}

This report applies to `{target}`. If you choose a different target in this step, do not treat it as a rejection of that unrelated work. Address the report when you next work on `{target}`.
'''.strip()

# Sticky reminder injected into every attempt after the first review
# rejection, built from harvested reviewer finding titles.  Unlike the full
# latest report, this remains useful after unrelated work lands.
AGENT_FFI_SEEN_FINDINGS_PROMPT = '''
Earlier attempts in this run or a prior run were rejected by review. The reviewer's findings included:

{findings}

Do not repeat these mistakes.
'''.strip()


def parse_verdict(final_message: str) -> tuple[str, str]:
    """
    The step verdict from the agent's final message: `('blocked', note)`,
    `('continue', handoff)`, or `('done', '')`.  The verdict must be the
    last non-empty line; anything else — including no verdict at all —
    reads as `done`, the only default that neither suppresses a target nor
    hands out extra invocations.
    """
    lines = [l.strip() for l in final_message.splitlines() if l.strip()]
    if lines:
        last = lines[-1]
        for kind, prefix in (('blocked', 'BLOCKED:'), ('continue', 'CONTINUE:')):
            if last.startswith(prefix):
                return kind, last[len(prefix):].strip()
    return 'done', ''

TARGET_LINE_RE = re.compile(r'^TARGET:\s*(\S+)\s*$', re.MULTILINE)

def parse_target(final_message: str) -> str | None:
    """The first `TARGET:` declaration in the agent's final message."""
    m = TARGET_LINE_RE.search(final_message)
    return m.group(1) if m else None

AGENT_AFTER_REFACTORING_RUN_TESTS = '''
After refactoring, make sure the code still passes the tests.  Run the tests using this script:
```sh
{test_cmd}
```
Run the full script once, before you declare DONE.  While work is in progress, `cargo build` and `cargo check-unsafe2` are the checks worth their cost; the full suite mid-step mostly re-proves what the build already shows.
Note: you MUST NOT edit the tests (or the original C code) to get them to pass.  Instead, you must ensure that your edits to the codebase preserve ALL externally-visible behavior that's exercised by the tests.
'''.strip()

AGENT_AFTER_REFACTORING_BUILD = '''
After refactoring, make sure the code still builds.
'''.strip()

AGENT_TARGET_GOAL_FIELD = '''
Your current target is the `{field_name}` field of `{struct_name}`, along with any similar or closely related fields.  Your goal is to change the field to use a safe type (such as `Box`, `Vec`, or `Rc`) instead of a raw pointer, and to update all uses of it to be as safe as possible (for example, use sites should not cast back to a raw pointer, since that defeats some of the safety checking).
'''.strip()

AGENT_TARGET_GOAL_FUNCTION = '''
Your current target is the `{func_name}` function, along with any similar or closely related functions.  Your goal is to change the function in question to use only safe types (such as `Box`, `Vec`, or `Rc`) internally and in its signature, and to replace any unsafe FFI calls (e.g. `libc::printf`) with safe equivalents.  You should aim to make callees of the target function safe if it's feasible to do so, that way the entire call tree is safe.
'''.strip()

class AgentTarget:
    def prompt(self):
        return self.PROMPT_FMT.format(**dataclasses.asdict(self))

@dataclass(frozen = True)
class AgentTargetField(AgentTarget):
    """
    Remove raw pointers from the type of a struct field.
    """
    struct_name: str
    field_name: str
    PROMPT_FMT = AGENT_TARGET_GOAL_FIELD

@dataclass(frozen = True)
class AgentTargetFunction(AgentTarget):
    """
    Make a function and all its callees safe.
    """
    func_name: str
    PROMPT_FMT = AGENT_TARGET_GOAL_FUNCTION

@dataclass(frozen = True)
class AgentTargetOther(AgentTarget):
    """
    Remove any leftover unsafe from the codebase.
    """
    # The prompt asks the agent to continue the plan, so we
    # expect it to make a reasonable choice for the next target
    # based on that document.
    PROMPT_FMT = ""


_CRISP_DIR = os.path.dirname(os.path.dirname(__file__))


@dataclass
class FuelCounter:
    desc: str
    default_give: int | None = None
    fuel: int = 0

    def __post_init__(self):
        if self.default_give is not None:
            self.give()

    def use(self):
        if self.fuel == 0:
            raise OutOfFuelError(self.desc)
        else:
            self.fuel -= 1

    def try_use(self) -> bool:
        if self.fuel == 0:
            return False
        else:
            self.fuel -= 1
            return True

    def is_empty(self) -> bool:
        return self.fuel == 0

    def give(self, amount: int | None = None):
        if amount is None:
            amount = self.default_give
        if amount is None:
            raise ValueError('must provide `amount` because `default_give` is None')
        if amount > self.fuel:
            self.fuel = amount

class OutOfFuelError(Exception):
    pass


def _print_step_value(prefix: str, x: Any):
    if isinstance(x, (tuple, list)):
        for i, y in enumerate(x):
            _print_step_value('%s[%d]' % (prefix, i), y)
    elif isinstance(x, dict):
        for k, v in x.items():
            _print_step_value('%s[%r]' % (prefix, k), v)
    else:
        if isinstance(x, Node):
            x = x.node_id()
        print('%s = %s' % (prefix, x))

def step(f):
    name = f.__name__
    sig = inspect.signature(f)

    @functools.wraps(f)
    def g(self, *args, **kwargs):
        bound = sig.bind(self, *args, **kwargs)
        if self._step_depth == 0:
            print(' ** ' + name)
        else:
            print(' ' * (1 + self._step_depth) + '* ' + name)
        for arg_name, val in bound.arguments.items():
            if isinstance(val, Workflow):
                continue
            _print_step_value(arg_name, val)

        self._step_depth += 1
        try:
            result = f(self, *args, **kwargs)
        finally:
            self._step_depth -= 1

        if result is not None:
            _print_step_value(name + ' result', result)
        return result

    return g


class Workflow:
    def __init__(self, cfg: Config, mvir: MVIR, codex_login: bool = False):
        self.cfg = cfg
        self.mvir = mvir
        self.fuel = FuelCounter('safety tries')
        self.codex_login = codex_login
        self._step_depth = 0

    def accept(self, code: TreeNode, reason = None):
        self.mvir.set_tag('current', code.node_id(), reason)

        if self.cfg.on_accept is not None:
            try:
                p = subprocess.run([self.cfg.on_accept], check=False)
            except OSError as e:
                print(f'warning: on-accept hook failed to run: {e}', file=sys.stderr)
            else:
                if p.returncode != 0:
                    print(
                        f'warning: on-accept hook exited with status {p.returncode}',
                        file=sys.stderr,
                    )
                    print(f'warning: on-accept hook cwd: {os.getcwd()}', file=sys.stderr)

    @step
    def cc_custom(self, c_code: TreeNode, artifact: str | int | None = None) -> FileNode:
        n_op_cc = self.cc_custom_op(c_code, artifact = artifact)
        if n_op_cc.exit_code != 0:
            raise CrispError(f"cc_custom_op failed with exit code {n_op_cc.exit_code}; "
                "ensure all needed c_code files were committed", n_op_cc)
        assert n_op_cc.compile_commands is not None
        compile_commands = self.mvir.node(n_op_cc.compile_commands)
        return compile_commands

    @step
    def cc_custom_op(
        self,
        c_code: TreeNode,
        artifact: str | int | None = None,
    ) -> CompileCommandsOpNode:
        cfg, mvir = self.cfg, self.mvir
        art_cfg = cfg.transpile.artifact(artifact)
        return analysis.cc_custom(cfg, mvir, c_code, art_cfg)

    @step
    def transpile(
        self,
        c_code: TreeNode,
        src_loc_annotations: bool = False,
        refactor_transforms: tuple[str, ...] = (),
        hayroll: bool = False,
    ) -> TreeNode:
        cfg, mvir = self.cfg, self.mvir

        # Count the number of artifacts to transpile.  This excludes artifacts
        # that are generated using `lib_from_bin_artifact`.  If there's only
        # one transpiled artifact, we put it at the top level of the output
        # directory as a non-virtual workspace root.
        num_transpiled_artifacts = sum(1 for a in cfg.transpile.artifacts
            if a.lib_from_bin_artifact is None)

        artifact_code = {}
        for i, art_cfg in enumerate(cfg.transpile.artifacts):
            art_name = art_cfg.name
            if art_cfg.lib_from_bin_artifact is None:
                compile_commands = self.cc_custom(c_code, artifact = art_name)
                if num_transpiled_artifacts > 1:
                    subdir = art_cfg.name
                else:
                    subdir = '.'
                n_op_transpile = self.transpile_cc_op(
                    c_code,
                    compile_commands,
                    artifact=i,
                    subdir=subdir,
                    src_loc_annotations=src_loc_annotations,
                    refactor_transforms=refactor_transforms,
                    hayroll=hayroll,
                )
                if n_op_transpile.rust_code is None:
                    print(f'error: transpile of {art_name} failed', file=sys.stderr)
                    return None
                art_code = mvir.node(n_op_transpile.rust_code)

                # Patch Cargo.toml before building and testing.  This makes sure we
                # test the code that will actually be used, and also gives
                # `patch_cargo_toml` a chance to fix the c2rust-bitflags dependency if
                # needed.
                art_code = self.patch_cargo_toml(art_code, name = art_name)

                # Add `-lcrypto` or similar flags if needed.
                if len(art_cfg.system_libs) > 0:
                    art_code = self.patch_build_rs(art_code, libs = art_cfg.system_libs)

            else:
                base_code = artifact_code[art_cfg.lib_from_bin_artifact]
                art_code = self.generate_lib_from_bin_cargo_toml(
                        base_code, art_name, subdir = art_name)

            artifact_code[art_name] = art_code

        all_files = {}
        path_origin = {}
        for art_name, art_code in artifact_code.items():
            for path, file in art_code.files.items():
                assert path not in all_files, \
                    f'artifacts {path_origin[path]!r} and {art_name!r} ' \
                    f'both contain file {path!r}'
                all_files[path] = file
                path_origin[path] = art_name
        code = TreeNode.new(mvir, files = all_files)

        code = self.patch_cargo_toml_workspace(code)

        # Note: the toolchain upgrade must run after c2rust-refactor, since
        # that tool only supports an older version of Rust.
        code = self.patch_upgrade_toolchain(code)

        if not self.cargo_check_json_op(code).passed:
            print('error: build failed after transpile')
            return None
        if not self.test(code, c_code):
            print('error: tests failed after transpile')
            return None
        return code

    @step
    def transpile_cc_op(
        self,
        n_c_code: TreeNode,
        n_cc: FileNode,
        artifact: str | int | None = None,
        subdir: str = '',
        src_loc_annotations: bool = False,
        refactor_transforms: tuple[str, ...] = (),
        hayroll: bool = False,
    ) -> TranspileOpNode:
        if "reorganize_definitions" in refactor_transforms:
            assert src_loc_annotations, (
                "reorganize_definitions requires src loc annotations"
            )
        if hayroll:
            assert len(refactor_transforms) == 0, (
                "refactor_transforms are not supported with hayroll yet"
            )

        if hayroll:
            # Hack: edit compile_commands.json to include `arguments` field
            import json, shlex
            j = n_cc.body_json()
            for x in j:
                if 'command' in x and 'arguments' not in x:
                    x['arguments'] = shlex.split(x['command'])
            n_cc = FileNode.new(self.mvir, json.dumps(j))

        cfg, mvir = self.cfg, self.mvir
        art_cfg = cfg.transpile.artifact(artifact)
        with run_sandbox(cfg, mvir) as sb:
            output_path = cfg.relative_path(os.path.join(cfg.transpile.output_dir, subdir))

            sb.checkout(n_c_code)
            sb.checkout_file(COMPILE_COMMANDS_PATH, n_cc)

            # Create each directory mentioned in compile_commands.json, since
            # c2rust may assume that they already exist.
            j = n_cc.body_json()
            sb_dir = sb.join()
            cc_dirs = {os.path.relpath(x['directory'], sb_dir)
                for x in j if 'directory' in x}
            for d in cc_dirs:
                sb.run(['mkdir', '-p', d])

            # Run c2rust-transpile
            if not hayroll:
                sb.run(['mkdir', '-p', output_path])

                c2rust_cmd = [
                    "c2rust",
                    "transpile",
                    sb.join(COMPILE_COMMANDS_PATH),
                    "--output-dir",
                    sb.join(output_path),
                    "--emit-build-files",
                ]
                if src_loc_annotations:
                    c2rust_cmd += [
                        "--reorganize-definitions",
                        "--disable-refactoring",
                    ]
                if art_cfg.bin_main is not None:
                    c2rust_cmd.extend((
                        '--binary', art_cfg.bin_main,
                        '--thin-binaries',
                        ))
                exit_code, logs = sb.run(c2rust_cmd)

                for transform in refactor_transforms:
                    if exit_code == 0:
                        c2rust_refactor_cmd = [
                            "c2rust",
                            "refactor",
                            "--cargo",
                            "--rewrite-mode",
                            "inplace",
                            transform,
                        ]
                        new_exit_code, new_logs = sb.run(
                            c2rust_refactor_cmd, cwd=output_path
                        )
                        exit_code = new_exit_code
                        logs += new_logs

                if exit_code == 0:
                    new_exit_code, new_logs = sb.run(["cargo", "clean"], cwd=output_path)
                    exit_code = new_exit_code
                    logs += new_logs

            else:
                project_dir_rel = cfg.relative_path(art_cfg.hayroll_project_dir)

                # Setting `--project-dir` explicitly prevents Hayroll from
                # including various ancestor directories as intermediate
                # modules.  We want it to translate `src/lib.c` to `src/lib.rs`
                # rather than `foo/bar/baz/src/lib.rs` because overly long file
                # paths sometimes confuse weaker LLMs.
                c2rust_cmd = [
                        'hayroll',
                        sb.join(COMPILE_COMMANDS_PATH),
                        sb.join(output_path),
                        '--project-dir', project_dir_rel,
                        ]
                # hayroll already has c2rust-transpile emit src loc annotations.
                if art_cfg.bin_main is not None:
                    c2rust_cmd.extend((
                        '--binary', art_cfg.bin_main,
                        '--thin-binaries',
                        ))
                exit_code, logs = sb.run(c2rust_cmd)

                if exit_code == 0:
                    exit_code, logs2 = sb.run([
                        'find', sb.join(output_path), '-name', '*.*.*', '-delete',
                    ])
                    logs = b'\n\n'.join((logs, logs2))

            if exit_code == 0:
                n_rust_code = sb.commit_dir(output_path)
            else:
                n_rust_code = None
            n_rust_code_id = n_rust_code.node_id() if n_rust_code is not None else None

        n_op = TranspileOpNode.new(
            mvir,
            body = logs,
            compile_commands = n_cc.node_id(),
            c_code = n_c_code.node_id(),
            cmd = c2rust_cmd,
            exit_code = exit_code,
            rust_code = n_rust_code_id,
            )
        mvir.set_tag('op_history', n_op.node_id(), n_op.kind)

        if exit_code != 0:
            # TODO: proper log parsing
            print(logs.decode())
        print('c2rust process %s with code %d:\n%s' % (
            'succeeded' if n_op.exit_code == 0 else 'failed', n_op.exit_code, n_op.cmd))

        return n_op

    @step
    def patch_cargo_toml(self, code: TreeNode, name: str | None = None) -> TreeNode:
        n_op = self.patch_cargo_toml_op(code, name = name)
        new_code = self.mvir.node(n_op.new_code)
        return new_code

    @step
    def patch_cargo_toml_op(self, code: TreeNode, name: str | None = None) -> EditOpNode:
        cfg, mvir = self.cfg, self.mvir

        if name is None:
            name = cfg.project_name

        cargo_toml_paths = [k for k in code.files.keys()
                if os.path.basename(k) == 'Cargo.toml']
        assert len(cargo_toml_paths) == 1, (
                f'expected only 1 Cargo.toml in transpiler output, but got {cargo_toml_paths}')
        cargo_toml_path, = cargo_toml_paths
        cargo_toml = mvir.node(code.files[cargo_toml_path])

        t = toml.loads(cargo_toml.body_str())

        if 'bin' in t:
            kind = 'bin'
            t['package']['name'] = name
            assert isinstance(t['bin'], list)
            assert len(t['bin']) == 1
            t['bin'][0]['name'] = name
        else:
            kind = 'lib'
            t['package']['name'] = name
            t['lib']['name'] = name
            t['lib']['crate-type'] = ['cdylib']

        new_files = code.files.copy()
        new_files[cargo_toml_path] = FileNode.new(mvir, toml.dumps(t)).node_id()
        new_code = TreeNode.new(mvir, files = new_files)

        n_op = EditOpNode.new(
            mvir,
            old_code = code.node_id(),
            new_code = new_code.node_id(),
            body = f'patch Cargo.toml (kind = {kind})',
        )
        mvir.set_tag('op_history', n_op.node_id(), n_op.kind + ' patch_cargo_toml')
        return n_op

    @step
    def generate_lib_from_bin_cargo_toml(
        self,
        base_code: TreeNode,
        name: str,
        subdir: str,
    ) -> TreeNode:
        cfg, mvir = self.cfg, self.mvir

        if name is None:
            name = cfg.project_name

        base_cargo_toml_paths = [k for k in base_code.files.keys()
                if os.path.basename(k) == 'Cargo.toml']
        assert len(base_cargo_toml_paths) == 1, (
            f'expected only 1 Cargo.toml in transpiler output, but got {base_cargo_toml_paths}')
        base_cargo_toml_path, = base_cargo_toml_paths
        base_cargo_dir = os.path.dirname(base_cargo_toml_path)
        base_cargo_toml = mvir.node(base_code.files[base_cargo_toml_path])

        new_cargo_dir = cfg.relative_path(os.path.join(cfg.transpile.output_dir, subdir))
        new_cargo_toml_path = os.path.join(new_cargo_dir, 'Cargo.toml')

        t = toml.loads(base_cargo_toml.body_str())

        t['package']['name'] = name

        # The base Cargo.toml will have a `lib` containing all the relevant
        # code, and a `bin` that wraps it and calls `lib::main`.  We discard
        # the `bin` section, and read out the `lib` so we can create a modified
        # version of it.
        t_lib = t['lib']
        del t['bin']

        # Use the baseline `lib` section to create the `lib` section for the
        # derived artifact.
        base_cargo_dir_rel = os.path.relpath(base_cargo_dir, new_cargo_dir)
        t['lib'] = {
            'path': os.path.join(base_cargo_dir_rel, t_lib['path']),
            'name': name,
            'crate-type': ['cdylib'],
        }

        new_files = {
            new_cargo_toml_path: FileNode.new(mvir, toml.dumps(t)).node_id(),
        }
        base_build_rs_path = os.path.join(base_cargo_dir, 'build.rs')
        if base_build_rs_path in base_code.files:
            new_build_rs_path = os.path.join(new_cargo_dir, 'build.rs')
            new_files[new_build_rs_path] = base_code.files[base_build_rs_path]
        new_code = TreeNode.new(mvir, files = new_files)

        return new_code

    @step
    def patch_cargo_toml_workspace(self, code: TreeNode) -> TreeNode:
        n_op = self.patch_cargo_toml_workspace_op(code)
        new_code = self.mvir.node(n_op.new_code)
        return new_code

    @step
    def patch_cargo_toml_workspace_op(self, code: TreeNode) -> EditOpNode:
        cfg, mvir = self.cfg, self.mvir

        workspace_dir = cfg.relative_path(cfg.transpile.output_dir)
        workspace_cargo_toml_path = os.path.join(workspace_dir, 'Cargo.toml')

        cargo_toml_paths = [k for k in code.files.keys()
                if os.path.basename(k) == 'Cargo.toml']

        all_t = {path: toml.loads(mvir.node(code.files[path]).body_str())
             for path in cargo_toml_paths}

        # Remove workspace sections from all Cargo.toml files
        for t in all_t.values():
            t.pop('workspace', None)

        members = [os.path.relpath(os.path.dirname(path), workspace_dir)
            for path in cargo_toml_paths
            if path != workspace_cargo_toml_path]

        if workspace_cargo_toml_path in code.files:
            # Add workspace options to the existing Cargo.toml
            t = all_t[workspace_cargo_toml_path]
            t['workspace'] = {
                'members': members,
                'default-members': ['.'] + members,
            }
        else:
            # Generate a virtual workspace manifest
            all_t[workspace_cargo_toml_path] = {
                'workspace': {
                    'members': members,
                    'default-members': members,
                },
            }


        new_files = code.files.copy()
        for path, t in all_t.items():
            new_files[path] = FileNode.new(mvir, toml.dumps(t)).node_id()

        # Add a rust-toolchain.toml to the top-level workspace if needed.
        workspace_toolchain_path = os.path.join(workspace_dir, 'rust-toolchain.toml')
        if workspace_toolchain_path not in code.files:
            toolchain_paths = [k for k in code.files.keys()
                    if os.path.basename(k) == 'rust-toolchain.toml']
            if len(toolchain_paths) > 0:
                # All toolchain files must be identical.
                toolchain_file_id = code.files[toolchain_paths[0]]
                assert all(code.files[path] == toolchain_file_id
                    for path in toolchain_paths)
                # Copy the toolchain file into the new workspace root.
                new_files[workspace_toolchain_path] = toolchain_file_id

        new_code = TreeNode.new(mvir, files = new_files)

        n_op = EditOpNode.new(
            mvir,
            old_code = code.node_id(),
            new_code = new_code.node_id(),
            body = f'patch Cargo.toml files to create workspace',
        )
        mvir.set_tag('op_history', n_op.node_id(), n_op.kind + ' patch_cargo_toml_workspace')
        return n_op

    @step
    def patch_build_rs(self, code: TreeNode, libs: list[str]) -> TreeNode:
        n_op = self.patch_build_rs_op(code, libs)
        new_code = self.mvir.node(n_op.new_code)
        return new_code

    @step
    def patch_build_rs_op(self, code: TreeNode, libs: list[str]) -> EditOpNode:
        cfg, mvir = self.cfg, self.mvir

        build_rs_paths = [k for k in code.files.keys()
                if os.path.basename(k) == 'build.rs']
        assert len(build_rs_paths) == 1, (
                f'expected only 1 build.rs in transpiler output, but got {build_rs_paths}')
        build_rs_path, = build_rs_paths
        build_rs = mvir.node(code.files[build_rs_path])

        new_build_rs_lines = ['fn main() {']
        for lib in libs:
            new_build_rs_lines.append(f'    println!("cargo:rustc-link-lib={lib}");')
        new_build_rs_lines.append('}\n')
        new_build_rs_src = '\n'.join(new_build_rs_lines)

        new_files = code.files.copy()
        new_files[build_rs_path] = FileNode.new(mvir, new_build_rs_src).node_id()
        new_code = TreeNode.new(mvir, files = new_files)

        n_op = EditOpNode.new(
            mvir,
            old_code = code.node_id(),
            new_code = new_code.node_id(),
            body = f'patch build.rs (libs = {libs})',
        )
        mvir.set_tag('op_history', n_op.node_id(), n_op.kind + ' patch_build_rs')
        return n_op

    @step
    def patch_upgrade_toolchain(self, code: TreeNode) -> TreeNode:
        """
        Patch `rust-toolchain.toml` and `.rs` files to make the project build
        with a newer toolchain.  c2rust-transpile outputs code for a 2023
        toolchain by default, but find-unsafe2 uses a newer 2026 toolchain;
        this upgrades the project to work with the latter.

        This is necessary because c2rust-transpile now outputs calls to the
        strict provenance APIs, which were renamed in 2024, making the
        transpiler output incompatible with find-unsafe2.  Upgrading to the
        newer toolchain (and renaming the strict provenance calls in the
        process) allows find-unsafe2 to work on the transpiled code.
        """
        n_op = self.patch_upgrade_toolchain_op(code)
        new_code = self.mvir.node(n_op.new_code)
        return new_code

    @step
    def patch_upgrade_toolchain_op(self, code: TreeNode) -> EditOpNode:
        cfg, mvir = self.cfg, self.mvir

        new_files = {}
        for path, file_id in code.files.items():
            if path.endswith('.rs'):
                file = mvir.node(file_id)
                old_src = file.body_str()
                new_src = (old_src
                    # This handles both `from_exposed_addr` and `from_exposed_addr_mut`
                    .replace('ptr::from_exposed_addr', 'ptr::with_exposed_provenance')
                    .replace('.expose_addr()', '.expose_provenance()')
                    # `raw_ref_op` no longer requires a feature gate
                    .replace('#![feature(raw_ref_op)]', '')
                    )
                new_file = FileNode.new(mvir, new_src)
                new_file_id = new_file.node_id()

            elif path.endswith('rust-toolchain.toml'):
                file = mvir.node(file_id)
                OLD_TOOLCHAIN = 'nightly-2023-04-15'
                NEW_TOOLCHAIN = 'nightly-2026-06-17'
                old_src = file.body_str()
                assert OLD_TOOLCHAIN in old_src, f'old toolchain toml = {old_src!r}'
                new_src = old_src.replace(OLD_TOOLCHAIN, NEW_TOOLCHAIN)
                new_file = FileNode.new(mvir, new_src)
                new_file_id = new_file.node_id()

            else:
                new_file_id = file_id

            new_files[path] = new_file_id

        new_code = TreeNode.new(mvir, files = new_files)

        n_op = EditOpNode.new(
            mvir,
            old_code = code.node_id(),
            new_code = new_code.node_id(),
            body = f'patch to upgrade toolchain',
        )
        mvir.set_tag('op_history', n_op.node_id(), n_op.kind + ' patch_upgrade_toolchain')
        return n_op

    @step
    def test(self, code: TreeNode, c_code: TreeNode) -> bool:
        n = self.test_op(code, c_code)
        return n.exit_code == 0

    @step
    def test_op(self, code: TreeNode, c_code: TreeNode) -> TestResultNode:
        test_cmd = self.cfg.test_command
        if test_cmd is None:
            test_cmd = 'true'
        n = analysis.run_tests(self.cfg, self.mvir, code, c_code, test_cmd)
        return n

    @step
    def cargo_fix(self, code: TreeNode) -> TreeNode:
        n = self.cargo_fix_op(code)
        return self.mvir.node(n.new_code)

    @step
    def cargo_fix_op(self, code: TreeNode) -> CargoFixOpNode:
        return analysis.cargo_fix(self.cfg, self.mvir, code)

    @step
    def cargo_check_json(self, code: TreeNode) -> list[dict]:
        n = self.cargo_check_json_op(code)
        n_json = self.mvir.node(n.json)
        return n_json.body_json()

    @step
    def cargo_check_json_op(self, code: TreeNode) -> CargoCheckJsonAnalysisNode:
        n = analysis.cargo_check_json(self.cfg, self.mvir, code)
        return n

    @step
    def inline_errors(self, code: TreeNode) -> TreeNode:
        n = self.inline_errors_op(code)
        return self.mvir.node(n.new_code)

    @step
    def inline_errors_op(self, code: TreeNode) -> CargoCheckJsonAnalysisNode:
        n_check_op = self.cargo_check_json_op(code)
        n_check_json = self.mvir.node(n_check_op.json)
        n = analysis.inline_errors(self.cfg, self.mvir, code, n_check_json)
        return n

    @step
    def count_unsafe(self, n_code: TreeNode) -> int:
        n_find_unsafe = self.find_unsafe_op(n_code)
        j_unsafe = n_find_unsafe.body_json()
        unsafe_count = sum(
            len(file_info['internal_unsafe_fns']) +
            len(file_info['fns_containing_unsafe']) +
            len(file_info['statics_containing_unsafe']) +
            len(file_info['mutable_statics']) +
            len(file_info['global_macro_invocations_containing_unsafe']) +
            len(file_info['macro_definitions_containing_unsafe'])
            for file_info in j_unsafe.values())
        print('%d unsafe functions remaining' % unsafe_count)
        return unsafe_count

    @step
    def find_unsafe_op(self, n_code: TreeNode) -> FindUnsafeAnalysisNode:
        return analysis.find_unsafe(self.cfg, self.mvir, n_code)

    @step
    def count_unsafe2(self, n_code: TreeNode) -> int:
        n_json = self.find_unsafe2_json(n_code)
        total = 0
        for n_json_file in self.find_unsafe2_json_files(n_code):
            total += n_json_file.body_json()['total_unsafe']
        print('%d unsafe operations remaining' % total)
        return total

    @step
    def find_unsafe2_json(self, n_code: TreeNode) -> TreeNode:
        n_op = self.find_unsafe2_op(n_code)
        return self.mvir.node(n_op.unsafe_json)

    @step
    def find_unsafe2_json_files(self, n_code: TreeNode) -> list[FileNode]:
        n_json = self.find_unsafe2_json(n_code)
        return [self.mvir.node(file_id) for file_id in n_json.files.values()]

    @step
    def find_unsafe2_op(self, n_code: TreeNode) -> FindUnsafe2AnalysisNode:
        return analysis.find_unsafe2(self.cfg, self.mvir, n_code)

    @step
    def check_unsafe2_op(
            self, n_code: TreeNode, n_unsafe_json: TreeNode) -> CheckUnsafe2AnalysisNode:
        return analysis.check_unsafe2(self.cfg, self.mvir, n_code, n_unsafe_json)

    def fn_records(self, n_code: TreeNode) -> dict:
        """Merged `fns` records across all crates' unsafety inventories."""
        out = {}
        for n_file in self.find_unsafe2_json_files(n_code):
            out.update(n_file.body_json().get('fns', {}))
        return out

    def type_records(self, n_code: TreeNode) -> dict:
        """Merged `types` records across all crates' unsafety inventories."""
        out = {}
        for n_file in self.find_unsafe2_json_files(n_code):
            out.update(n_file.body_json().get('types', {}))
        return out

    @step
    def compare_unsafe2_op(
            self, n_old_code: TreeNode, n_new_code: TreeNode) -> CheckUnsafe2AnalysisNode:
        n_find_op = self.find_unsafe2_op(n_old_code)
        n_unsafe_json = self.mvir.node(n_find_op.unsafe_json)
        return self.check_unsafe2_op(n_new_code, n_unsafe_json)

    @step
    def llm_safety(
        self,
        n_code: TreeNode,
        prompt: str = LLM_SAFETY_PROMPT,
    ) -> TreeNode:
        n_new_code, n_op_llm = self.llm_safety_op(n_code, prompt = prompt)
        return n_new_code

    @step
    def llm_safety_op(
        self,
        n_code: TreeNode,
        prompt: str = LLM_SAFETY_PROMPT,
    ) -> tuple[TreeNode, LlmOpNode]:
        return llm.run_rewrite(
                self.cfg, self.mvir, prompt, n_code,
                glob_filter = self.cfg.src_globs)

    @step
    def llm_gepa(
        self,
        n_code: TreeNode,
        prompt: str,
    ) -> TreeNode:
        n_new_code, _ = llm.run_rewrite(
            self.cfg, self.mvir, prompt, n_code,
            glob_filter = self.cfg.src_globs,
            separate_system_prompt = True
        )
        return n_new_code

    @step
    def llm_repair(self, n_code: TreeNode, n_op_test: TestResultNode) -> TreeNode:
        n_new_code, n_op_llm = self.llm_repair_op(n_code, n_op_test)
        return n_new_code

    @step
    def llm_repair_op(self, n_code: TreeNode,
            n_op_test: TestResultNode) -> tuple[TreeNode, LlmOpNode]:
        return llm.run_rewrite(
                self.cfg, self.mvir, LLM_REPAIR_PROMPT, n_code,
                glob_filter = self.cfg.src_globs,
                format_kwargs = {'test_output': n_op_test.body_str()},
                think = True)

    @step
    def llm_repair_compile(
        self,
        n_code: TreeNode,
        n_op_check: CargoCheckJsonAnalysisNode,
    ) -> TreeNode:
        n_new_code, n_op_llm = self.llm_repair_compile_op(n_code, n_op_check)
        return n_new_code

    @step
    def llm_repair_compile_op(
        self,
        n_code: TreeNode,
        n_op_check: CargoCheckJsonAnalysisNode,
    ) -> tuple[TreeNode, LlmOpNode]:
        n_json = self.mvir.node(n_op_check.json)
        json_errors = n_json.body_json()
        stderr = ''.join(j['message']['rendered']
            for j in json_errors if j.get('reason') == 'compiler-message')
        return llm.run_rewrite(
                self.cfg, self.mvir, LLM_REPAIR_COMPILE_PROMPT, n_code,
                glob_filter = self.cfg.src_globs,
                format_kwargs = {'stderr': stderr},
                think = True)

    @step
    def llm_repair_safety(
        self,
        n_code: TreeNode,
        n_op_check: CargoCheckJsonAnalysisNode,
    ) -> TreeNode:
        n_new_code, n_op_llm = self.llm_repair_safety_op(n_code, n_op_check)
        return n_new_code

    @step
    def llm_repair_safety_op(
        self,
        n_code: TreeNode,
        n_op_check: CheckUnsafe2AnalysisNode,
    ) -> tuple[TreeNode, LlmOpNode]:
        return llm.run_rewrite(
                self.cfg, self.mvir, LLM_REPAIR_SAFETY_PROMPT, n_code,
                glob_filter = self.cfg.src_globs,
                format_kwargs = {'logs': n_op_check.body_str()},
                think = True)

    @step
    def split_ffi(self, n_tree: TreeNode) -> TreeNode:
        op = self.split_ffi_op(n_tree)
        return self.mvir.node(op.new_code)

    @step
    def split_ffi_op(self, n_tree: TreeNode) -> SplitFfiOpNode:
        cfg, mvir = self.cfg, self.mvir

        rust_path_rel = cfg.relative_path(cfg.transpile.output_dir)

        with run_sandbox(cfg, mvir) as sb:
            sb.checkout(n_tree)

            exit_code, logs = sb.run(['split_ffi_entry_points', sb.join(rust_path_rel)])

            if exit_code == 0:
                exit_code, logs2 = sb.run([
                    'cargo', 'fmt', '--manifest-path',
                    sb.join(rust_path_rel, 'Cargo.toml')])

                # `cargo fmt` sometimes fails with an internal error about
                # being unable to remove trailing whitespace (see issue #146).
                # We log the error in that case and then suppress it.
                if exit_code != 0:
                    logs2 += f'\n`cargo fmt` failed with code {exit_code}\n'.encode()
                    exit_code = 0

                logs = b'\n\n'.join((logs, logs2))

            if exit_code == 0:
                exit_code, logs2 = sb.run(['rm', '-rfv', sb.join(rust_path_rel, 'target')])
                logs = b'\n\n'.join((logs, logs2))

            if exit_code == 0:
                n_new_tree = sb.commit_dir(rust_path_rel)
            else:
                # TODO: record exit code in the `Node`, like `transpile_cc_op` does
                raise CrispError(
                    f'split_ffi_entry_points failed (exit code = {exit_code})\n'
                    f'logs:\n{logs.decode("utf-8", errors="replace")}')

        n_op = SplitFfiOpNode.new(
                mvir,
                old_code = n_tree.node_id(),
                new_code = n_new_tree.node_id(),
                body = logs,
                )
        mvir.set_tag('op_history', n_op.node_id(), n_op.kind)

        return n_op

    @step
    def split(self, n_code: TreeNode, root_file: str | None = None) -> CrateNode:
        n_op = self.split_op(n_code, root_file = root_file)
        return self.mvir.node(n_op.crate_out)

    @step
    def split_op(self, n_code: TreeNode, root_file: str | None = None) -> SplitOpNode:
        return analysis.split_rust(self.cfg, self.mvir, n_code, root_file = root_file)

    @step
    def merge(self, n_code: TreeNode, n_crate: CrateNode) -> TreeNode:
        n_op = self.merge_op(n_code, n_crate)
        return self.mvir.node(n_op.code_out)

    @step
    def merge_op(self, n_code: TreeNode, n_crate: CrateNode) -> MergeOpNode:
        return analysis.merge_rust(self.cfg, self.mvir, n_code, n_crate)

    @step
    def related_decls_op(
        self,
        n_code: TreeNode,
        query_def_names: list[str] | None = None,
    ) -> RelatedDeclsOpNode:
        return analysis.related_decls(self.cfg, self.mvir, n_code,
            query_def_names = query_def_names)

    def _filter_defs(self, code: TreeNode, f: Callable[[str], bool]) -> CrateNode:
        mvir = self.mvir

        crate = self.split(code)

        crate_erased = CrateNode.new(mvir,
            defs = {k: v for k, v in crate.defs.items() if f(k)})
        return crate_erased

    @step
    def erase_ffi(self, code: TreeNode) -> TreeNode:
        """
        Erase all FFI functions introduced by `split_ffi` from `code`.  They
        can be handled separately and re-inserted using `unerase_ffi`.
        """
        # For now, we assume any function whose name ends with `_ffi` is an FFI
        # entry point introduced by `split_ffi`.  It should be fairly rare for
        # the original C code to use such names itself, since FFI logic is
        # usually in a language-specific adapter rather than the core C
        # library.
        crate_erased = self._filter_defs(code, lambda k: not k.endswith('_ffi'))
        code_erased = self.merge(code, crate_erased)
        return code_erased

    @step
    def extract_ffi_defs(self, code: TreeNode) -> CrateNode:
        """
        Extract FFI function definitions from `code`.
        """
        return self._filter_defs(code, lambda k: k.endswith('_ffi'))

    @step
    def unerase_ffi(self,
            code_old: TreeNode, code_new: TreeNode, crate_ffi: CrateNode) -> TreeNode:
        mvir = self.mvir

        crate_new = self.split(code_new)

        defs_out = crate_new.defs.copy()
        for k, v in crate_ffi.defs.items():
            if k in defs_out:
                raise CrispError(f'{k!r} is present in both the ffi and non-ffi inputs')
            defs_out[k] = v
        crate_out = CrateNode.new(mvir, defs = defs_out)

        code_out = self.merge(code_old, crate_out)
        return code_out

    @step
    def llm_safety_no_ffi(self, orig_code: TreeNode) -> TreeNode:
        main_code = self.erase_ffi(orig_code)
        # TODO: alternate safety prompt
        new_main_code = self.llm_safety(main_code, prompt = LLM_SAFETY_PROMPT_NO_FFI)

        orig_sigs = self.extract_sigs(orig_code)
        main_sigs = self.extract_sigs(new_main_code)
        ffi_defs = self.extract_ffi_defs(orig_code)
        new_ffi_defs = self.llm_repair_call_sites(ffi_defs, orig_sigs, main_sigs)

        code = self.unerase_ffi(main_code, new_main_code, new_ffi_defs)
        return code

    @step
    def extract_sigs(self, code: TreeNode) -> CrateNode:
        cfg, mvir = self.cfg, self.mvir
        n_op = self.related_decls_op(code)
        return mvir.node(n_op.sigs_out)

    @step
    def llm_repair_call_sites(
        self,
        ffi_defs: CrateNode,
        old_sigs: CrateNode,
        new_sigs: CrateNode,
    ) -> CrateNode:
        cfg, mvir = self.cfg, self.mvir

        ffi_defs_list = [mvir.node(v) for k,v in ffi_defs.defs.items() if k.endswith('_ffi')]
        if len(ffi_defs_list) == 0:
            return CrateNode.new(mvir, defs = {})
        ffi_file = FileNode.new(mvir, '\n\n'.join(d.body_str() for d in ffi_defs_list))
        ffi_tree = TreeNode.new(mvir, files = {'ffi.rs': ffi_file.node_id()})

        old_sigs_str = '\n'.join(mvir.node(v).body_str() for v in old_sigs.defs.values())
        new_sigs_str = '\n'.join(mvir.node(v).body_str() for v in new_sigs.defs.values())

        new_ffi_tree, _llm_op = llm.run_rewrite(
                cfg, mvir, LLM_PROMPT_REPAIR_CALL_SITES, ffi_tree,
                format_kwargs = dict(
                    old_sigs = old_sigs_str,
                    new_sigs = new_sigs_str,
                ))

        # `new_ffi_tree` has a flat module structure; all FFI functions have
        # been renamed to be at top level, e.g. `foo::bar_ffi` -> `bar_ffi`.
        # We need to move these back to their respective paths.
        new_ffi_crate_renamed = self.split(new_ffi_tree, root_file = 'ffi.rs')
        new_ffi_defs_dct = {}
        for k in ffi_defs.defs.keys():
            _, _, def_name = k.rpartition('::')
            if def_name in new_ffi_crate_renamed.defs:
                new_ffi_defs_dct[k] = new_ffi_crate_renamed.defs[def_name]
            else:
                print(f'warning: LLM omitted FFI def {def_name!r}')
                # Copy the original version of this FFI function.  This will
                # likely cause a compile error, which `llm_repair_compile` will
                # try to fix.
                new_ffi_defs_dct[k] = ffi_defs.defs[k]
        new_ffi_defs = CrateNode.new(mvir, defs = new_ffi_defs_dct)

        return new_ffi_defs

    @step
    def agent_safety(
        self,
        n_code: TreeNode,
        n_test_code: TreeNode | None,
        n_plans: TreeNode,
        # If set, provide `cfg.test_command` to the agent, if it's available.
        provide_test_cmd: bool = True,
        prompt_suffix: str | None = None,
        target_goal: AgentTarget = AgentTargetOther(),
        # Baseline for the agent's in-sandbox `cargo check-unsafe2`; a
        # multi-invocation step pins this to the step's start so the check
        # reproduces the judgment the whole step will face.
        baseline_json: TreeNode | None = None,
        # Pre-rendered target menu; a multi-invocation step pins this to the
        # step's start alongside the baseline.
        menu_text: str | None = None,
    ) -> tuple[TreeNode, TreeNode, str]:
        cfg, mvir = self.cfg, self.mvir
        cargo_dir = cfg.relative_path(cfg.transpile.output_dir)

        if menu_text is None:
            menu_text = format_menu(
                self.fn_records(n_code), self.type_records(n_code))

        if provide_test_cmd and cfg.test_command is not None:
            after_refactoring_instruction = AGENT_AFTER_REFACTORING_RUN_TESTS \
                    .format(test_cmd = cfg.test_command)
        else:
            after_refactoring_instruction = AGENT_AFTER_REFACTORING_BUILD

        extra_code = {}
        if n_test_code is not None:
            extra_code['tests'] = n_test_code

        prompt = AGENT_SAFETY_PROMPT.format(
            cargo_dir_path = cargo_dir,
            after_refactoring_instruction = after_refactoring_instruction,
            target_goal = target_goal.prompt(),
            menu = menu_text,
            checker_rules = CHECKER_RULES,
            tolerated_unsafety_rules = TOLERATED_UNSAFETY_RULES,
            ffi_entry_point_rules = FFI_ENTRY_POINT_RULES,
        )
        if prompt_suffix is not None:
            prompt = f'{prompt}\n\n{prompt_suffix}'
        return agent.run_rewrite(cfg, mvir, prompt, self.cfg.models.agent_loop, n_code,
            extra_code = extra_code,
            planning_files = n_plans,
            unsafe_json = (baseline_json if baseline_json is not None
                else self.find_unsafe2_json(n_code)),
            codex_login=self.codex_login,
            clean_cmds = [
                ['cargo', 'clean', '--manifest-path', os.path.join(cargo_dir, 'Cargo.toml')],
            ],
            find_unsafe2_json_dir = analysis.UNSAFE_JSON_DIR,
            find_unsafe2_src_dir = cargo_dir,
        )

    def _run_codex_review(
        self,
        n_old_code: TreeNode,
        n_new_code: TreeNode,
        prompt: str,
    ) -> CodexReviewOpNode:
        cfg, mvir = self.cfg, self.mvir

        report, logs, ran_commands = agent.run_review(cfg, mvir, prompt,
            cfg.models.agent_review, n_old_code, n_new_code,
            codex_login = self.codex_login,
            effort = 'xhigh')

        if report.strip() == '':
            # Fail closed on a missing report.
            print('warning: review returned an empty report')
            passed = False
        elif not ran_commands:
            # Fail closed when the reviewer never successfully ran a command:
            # it cannot have inspected the diff, whatever the report says.
            print('warning: reviewer ran no commands; ignoring its report')
            passed = False
        else:
            passed = AGENT_FFI_REVIEW_FINDING_RE.search(report) is None

        n_op = CodexReviewOpNode.new(mvir,
            old_code = n_old_code.node_id(),
            new_code = n_new_code.node_id(),
            raw_prompt = FileNode.new(mvir, prompt).node_id(),
            report = FileNode.new(mvir, report).node_id(),
            verdict = 'PASS' if passed else 'FAIL',
            body = logs,
        )
        mvir.set_tag('op_history', n_op.node_id(), n_op.kind)
        return n_op

    @step
    def ffi_review_op(
        self,
        n_old_code: TreeNode,
        n_new_code: TreeNode,
    ) -> CodexReviewOpNode:
        cargo_dir = self.cfg.relative_path(self.cfg.transpile.output_dir)
        prompt = AGENT_FFI_REVIEW_PROMPT.format(
            cargo_dir_path = cargo_dir,
            ffi_entry_point_rules = FFI_ENTRY_POINT_RULES)
        return self._run_codex_review(n_old_code, n_new_code, prompt)

    @step
    def tolerated_review_op(
        self,
        n_old_code: TreeNode,
        n_new_code: TreeNode,
        warnings: str,
    ) -> CodexReviewOpNode:
        cargo_dir = self.cfg.relative_path(self.cfg.transpile.output_dir)
        prompt = AGENT_TOLERATED_REVIEW_PROMPT.format(
            cargo_dir_path = cargo_dir,
            warnings = warnings,
            tolerated_unsafety_rules = TOLERATED_UNSAFETY_RULES,
            ffi_entry_point_rules = FFI_ENTRY_POINT_RULES)
        return self._run_codex_review(n_old_code, n_new_code, prompt)

    @step
    def do_tolerated_review(
        self,
        n_old_code: TreeNode,
        n_new_code: TreeNode,
        n_op_unsafe: CheckUnsafe2AnalysisNode,
    ) -> tuple[bool, str | None]:
        """
        Review a change that `check-unsafe2` tolerated with warnings: per-
        function unsafety increases are legal when the global total doesn't
        grow, but a reviewer must confirm they aren't laundering.  Returns
        `(passed, report)` like `do_ffi_review`.
        """
        warnings = extract_checker_warnings(n_op_unsafe.body_str())
        if not warnings:
            return True, None

        n_op = self.tolerated_review_op(
            n_old_code, n_new_code, '\n'.join(warnings))
        report = self.mvir.node(n_op.report).body_str()
        print(report)
        if n_op.verdict == 'PASS':
            return True, None
        return False, report if report.strip() else None

    @step
    def do_ffi_review(
        self,
        n_old_code: TreeNode,
        n_new_code: TreeNode,
    ) -> tuple[bool, str | None]:
        """
        Diff-triggered FFI review: if the change touched any FFI entry point,
        have a reviewer agent check it against the FFI entry point rules.
        Returns `(passed, report)`; `report` is the reviewer's findings when
        the change is rejected, or `None` if there is no usable report.
        """
        old_defs = self.extract_ffi_defs(n_old_code).defs
        new_defs = self.extract_ffi_defs(n_new_code).defs
        if old_defs == new_defs:
            return True, None

        n_op = self.ffi_review_op(n_old_code, n_new_code)
        report = self.mvir.node(n_op.report).body_str()
        print(report)
        if n_op.verdict == 'PASS':
            return True, None
        return False, report if report.strip() else None

    @step
    def agent_safety_no_tests(
        self,
        n_code: TreeNode,
        n_plans: TreeNode,
    ) -> tuple[TreeNode, TreeNode, str]:
        return self.agent_safety(n_code, None, n_plans, provide_test_cmd = False)


    @step
    def do_validate_and_repair(
        self,
        n_old_code: TreeNode,
        n_new_code: TreeNode,
        n_test_code: TreeNode,
    ) -> TreeNode | None:
        """
        Validate `n_new_code`.  If it fails validation, try to repair it.
        Returns a version that passes validation (after zero or more repair
        attempts), or `None` if no passing version was found.
        """
        for repair_try in range(3):
            try:
                n_op_unsafe = self.compare_unsafe2_op(n_old_code, n_new_code)
                if n_op_unsafe.exit_code != 0:
                    # Unsafe check failed, so try to repair it.  If repair
                    # succeeds, we proceed to the next check; otherwise, we try
                    # again from the start of the loop.
                    n_new_code = self.llm_repair_safety(n_new_code, n_op_unsafe)

                    n_op_unsafe = self.compare_unsafe2_op(n_old_code, n_new_code)
                    if n_op_unsafe.exit_code != 0:
                        # If we failed to fix the unsafety, don't bother trying to
                        # build or run tests.  This still counts as a repair
                        # attempt.
                        continue

                n_op_check = self.cargo_check_json_op(n_new_code)
                if not n_op_check.passed:
                    n_new_code = self.llm_repair_compile(n_new_code, n_op_check)

                    n_op_check = self.cargo_check_json_op(n_new_code)
                    if not n_op_check.passed:
                        continue

                n_op_test = self.test_op(n_new_code, n_test_code)
                if n_op_test.exit_code != 0:
                    n_new_code = self.llm_repair(n_new_code, n_op_test)

                    n_op_test = self.test_op(n_new_code, n_test_code)
                    if n_op_test.exit_code != 0:
                        continue

                # All validation steps passed, so return the new version.
                return n_new_code

            except CrispError as e:
                print(f'repair attempt {repair_try} failed: {e}')
                traceback.print_exc()

        # None of the new versions passed the checks.
        return None

    @step
    def do_safety_step_llm(
        self,
        n_code: TreeNode,
        n_test_code: TreeNode,
        no_ffi: bool = False,
    ) -> TreeNode | None:
        """
        Run one LLM safety rewriting step.  Returns a new version that passes
        validation (as in `do_validate_and_repair`), or `None` if no passing
        version was found.
        """
        self.fuel.use()

        if no_ffi:
            n_new_code = self.llm_safety_no_ffi(n_code)
        else:
            n_new_code = self.llm_safety(n_code)

        return self.do_validate_and_repair(n_code, n_new_code, n_test_code)

    @step
    def do_safety_plan_agent(
        self,
        n_code: TreeNode,
        n_test_code: TreeNode,
    ) -> tuple[TreeNode, TreeNode, str]:
        cfg, mvir = self.cfg, self.mvir
        cargo_dir = cfg.relative_path(cfg.transpile.output_dir)

        extra_code = {}
        if n_test_code is not None:
            # In planning mode, we do provide the entire C code to the planner
            # since that might increase the quality of the plan. However, if
            # we later run the safety loop in `agent_sim_no_tests` mode the
            # safety agent will not have access to the C code, and the plan
            # might contain references to it. This might confuse the agent and
            # break the `agent_sim_no_tests` mode.
            # TODO: un-break that mode somehow (without hiding the C code).
            extra_code['c_code'] = n_test_code

        prompt = AGENT_PLAN_PROMPT.format(
            cargo_dir_path = cargo_dir,
            ffi_entry_point_rules = FFI_ENTRY_POINT_RULES)
        return agent.run_rewrite(cfg, mvir, prompt, self.cfg.models.agent_plan, n_code,
            extra_code = extra_code,
            unsafe_json = self.find_unsafe2_json(n_code),
            planning_files = None,
            effort = 'high',
            codex_login=self.codex_login,
            codex_agents=agent.PLANNING_CODEX_AGENTS,
            clean_cmds = [
                ['cargo', 'clean', '--manifest-path', os.path.join(cargo_dir, 'Cargo.toml')],
            ],
            find_unsafe2_json_dir = analysis.UNSAFE_JSON_DIR,
            find_unsafe2_src_dir = cargo_dir,
        )

    @step
    def do_safety_step_agent(
        self,
        n_code: TreeNode,
        n_test_code: TreeNode,
        n_plans: TreeNode,
        prompt_suffix: str | None = None,
        target_goal: AgentTarget = AgentTargetOther(),
        max_invocations: int = 1,
        suppressed: frozenset[str] = frozenset(),
    ) -> StepOutcome:
        """
        Run one safety step of up to `max_invocations` agent invocations and
        judge the result once, against `n_code`.  A `CONTINUE` verdict
        extends the step: the intermediate tree may regress the unsafe
        counts, but must build to become the checkpoint the next invocation
        resumes from.  The judged battery is unchanged from a
        single-invocation step, and only a landing candidate pays for it.
        """
        n_base = n_code
        # Pin the checker baseline to the step's start: the agent's own
        # `cargo check-unsafe2` then reproduces the final judgment exactly,
        # instead of comparing against its latest checkpoint.
        n_base_json = self.find_unsafe2_json(n_base)
        menu_text = format_menu(
            self.fn_records(n_base), self.type_records(n_base), suppressed)
        n_cur = n_code
        target = None
        invocations = 0
        handoff = None
        for i in range(max_invocations):
            self.fuel.use()
            invocations += 1
            parts = [p for p in (prompt_suffix, handoff) if p]
            if i + 1 == max_invocations and max_invocations > 1:
                parts.append(AGENT_FINAL_INVOCATION_NOTE)
            n_next, _, final_message = self.agent_safety(
                n_cur, n_test_code, n_plans,
                prompt_suffix = '\n\n'.join(parts) if parts else None,
                target_goal = target_goal,
                baseline_json = n_base_json,
                menu_text = menu_text)
            if target is None:
                target = parse_target(final_message)
            verdict, note = parse_verdict(final_message)
            if verdict == 'blocked':
                # No legal reduction here.  Keep the plan updates, discard
                # the code edits; the accepted code is unchanged.
                print(f'agent declared blocked: {note}')
                return StepOutcome(n_base, n_plans, None, target,
                    note, invocations)
            if verdict == 'done':
                n_cur = n_next
                break
            if not self.cargo_check_json_op(n_next).passed:
                # A checkpoint that does not build rejects the whole step.
                return StepOutcome(None, None, None, target, '',
                    invocations)
            n_cur = n_next
            handoff = AGENT_CONTINUE_HANDOFF.format(note = note)
        if n_cur.node_id() == n_base.node_id():
            # No invocation produced a usable tree; nothing to judge.
            return StepOutcome(n_base, n_plans, None, target, '',
                invocations)
        # The step must pass tests, must not regress the unsafe counts, must
        # pass review of any tolerated unsafety moves, and must not break the
        # FFI entry point rules — all judged against the step's start.  The
        # cheap unsafe comparison runs first so a regressing attempt doesn't
        # pay for the full test suite.
        n_op_unsafe = self.compare_unsafe2_op(n_base, n_cur)
        if n_op_unsafe.exit_code != 0:
            return StepOutcome(None, None, None, target, '', invocations)
        n_op_test = self.test_op(n_cur, n_test_code)
        if n_op_test.exit_code != 0:
            return StepOutcome(None, None, None, target, '', invocations)
        tol_ok, tol_report = self.do_tolerated_review(
            n_base, n_cur, n_op_unsafe)
        if not tol_ok:
            return StepOutcome(None, None, tol_report, target, '', invocations)
        ffi_ok, ffi_report = self.do_ffi_review(n_base, n_cur)
        if not ffi_ok:
            # Surface the reviewer's report so the caller can feed it back
            # into the next attempt's prompt.
            return StepOutcome(None, None, ffi_report, target, '', invocations)
        return StepOutcome(n_cur, n_plans, None, target, '', invocations)

    @step
    def do_safety_step_agent_sim_no_tests(
        self,
        n_code: TreeNode,
        n_test_code: TreeNode,
        n_plans: TreeNode,
    ) -> tuple[TreeNode | None, TreeNode | None]:
        self.fuel.use()

        # Don't provide the test code, so the agent can't
        # accidentally find the tests.  Note this has the side
        # effect of not providing the original C code, since we
        # don't currently distinguish test code from the rest of
        # the C code.
        n_new_code, _, final_message = self.agent_safety_no_tests(
            n_code, n_plans)
        if parse_verdict(final_message)[0] == 'blocked':
            print(f'agent declared blocked: {final_message.strip()}')
            return n_code, n_plans
        n_op_check = self.cargo_check_json_op(n_new_code)
        n_op_unsafe = self.compare_unsafe2_op(n_code, n_new_code)
        if not (n_op_check.passed and n_op_unsafe.exit_code == 0):
            return None, None
        if not self.do_tolerated_review(n_code, n_new_code, n_op_unsafe)[0]:
            return None, None
        if not self.do_ffi_review(n_code, n_new_code)[0]:
            return None, None

        # `agent_sim_no_tests` simulates the mode where no tests are
        # available and the only success criteria that CRISP can
        # check are whether the code builds or not.  We actually do
        # run the tests here, but if the accepted `n_code` ever
        # fails the tests, we bail out, on the assumption that
        # actually running CRISP with no tests on this input would
        # cause it to produce non-working code.
        n_op_test = self.test_op(n_new_code, n_test_code)
        assert n_op_test.exit_code == 0, \
            f'agent output failed tests: {n_op_test}'

        return n_new_code, n_plans
