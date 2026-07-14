import cbor
import dataclasses
from dataclasses import dataclass
from datetime import datetime
import functools
import inspect
import os
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
    LlmOpNode, TestResultNode, FindUnsafeAnalysisNode, SplitFfiOpNode,
    CargoCheckJsonAnalysisNode, EditOpNode, WorkflowStepInputsNode,
    WorkflowStepNode, SplitOpNode, MergeOpNode, CrateNode, DefNode,
    RelatedDeclsOpNode, FindUnsafe2AnalysisNode, CheckUnsafe2AnalysisNode,
    CargoFixOpNode,
)
from .sandbox import run_sandbox
from .work_dir import lock_work_dir


# Whether to cache the results of workflow steps.  This is more aggressive than
# the built-in caching of the `analysis` module because it will even cache the
# results of nondeterministic steps like LLM calls.  This is useful during
# development, particularly when working on a step later in the pipeline,
# because it allows skipping over all of the prior steps and going directly to
# the step of interest.
USE_WORKFLOW_CACHE = int(os.environ.get('CRISP_USE_WORKFLOW_CACHE') or 0) != 0


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

AGENT_PLAN_PROMPT = '''
Make a plan to refactor the Rust code in `{cargo_dir_path}` to be safe, without changing its behavior.  The goal is to make the code fully safe, including migrating to safe memory management (`Box`/`Vec`/`Rc`) and  safe pointer types (`&T`/`&[T]`) throughout.
Write the plan to a file named `SAFETY_PLAN.md`.

The code contains two kinds of functions: implementation functions and FFI entry points. A function is an FFI entry point if it has the `#[no_mangle]` or `#[export_name]` attribute ("export attributes"). Note that just having `unsafe extern "C"` qualifiers without export attributes does NOT make a function an FFI entry point. All functions without export attributes are implementation functions.

Implementation code may be freely refactored to improve safety. Here are some examples of useful changes to make:
- Replace raw pointers with safe reference or smart pointer types, so that dereferences can be done safely.
- Replace `static mut`s with non-`mut` and unions with enums so they can be accessed safely.
- Eliminate calls to FFI imports and other unsafe functions. Try to replace these with suitable safe Rust operations, e.g. `printf` -> `println!`.
- Remove the `unsafe` qualifier from functions that no longer need it, so that their callers can be made safe.

For FFI entry points, the following rules apply (these rules must be copied verbatim into the plan and never changed or broken):
- You must not change the signature. FFI entry point signatures must remain exactly as-is to ensure ABI compatibility with the current version of the code. Don't remove `unsafe` or `extern "C"` qualifiers from FFI entry points.
- For struct types that appear only behind a pointer, you may assume the struct is opaque to the user of the library (unless otherwise indicated within the code itself), as is considered best practice in C. This means the struct layout is not part of the ABI, so you may freely change the field types to improve safety.
- Each FFI entry point should convert the inputs from unsafe types to safe ones (e.g. `*const T` -> `&T`) if needed, dispatch to an implementation function, and convert the results back to unsafe types if needed. Do not add extraneous unsafe code to FFI entry points.

{after_refactoring_instruction}

Your changes must not introduce new unsafe code within implementation functions. You can check your work using this command:
```sh
cargo check-unsafe2 --manifest-path {cargo_dir_path}/Cargo.toml
```
This will report an error for any unsafe code that was improperly added during your edits. It also reports errors on any newly added "unsafe-adjacent" code, including int-to-pointer casts and arguments or fields of raw pointer type.
'''

AGENT_SAFETY_PROMPT = '''
Continue the plan from `SAFETY_PLAN.md`.
**Before you finish, update `SAFETY_PLAN.md`** to reflect what you actually did this iteration, what is now complete, what remains, and any pitfalls or dead ends future iterations should avoid. Keep it concise — it is a working scratchpad, not a report.

{target_goal}

{after_refactoring_instruction}

Your changes must not introduce new unsafe code within implementation functions. You can check your work using this command:
```sh
cargo check-unsafe2 --manifest-path {cargo_dir_path}/Cargo.toml
```
This will report an error for any unsafe code that was improperly added during your edits. It also reports errors on any newly added "unsafe-adjacent" code, including int-to-pointer casts and arguments or fields of raw pointer type.
'''

AGENT_AFTER_REFACTORING_RUN_TESTS = '''
After refactoring, make sure the code still passes the tests.  Run the tests using this script:
```sh
{test_cmd}
```
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
    fuel: int = 0

    def use(self):
        if self.fuel == 0:
            raise OutOfFuelError(self.desc)
        else:
            self.fuel -= 1

    def give(self, amount):
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
    ann = typing.get_type_hints(f)
    return_type = ann['return']
    can_cache = isinstance(return_type, type) and issubclass(return_type, Node)
    if not USE_WORKFLOW_CACHE:
        can_cache = False

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

        mvir = self.mvir
        n_step = None
        if can_cache:
            # Look for a cached node for this step.
            inputs = [(k, v.node_id().to_cbor() if isinstance(v, Node) else v)
                      for k, v in bound.arguments.items()
                      if not isinstance(v, Workflow)]
            n_inputs = WorkflowStepInputsNode.new(
                    mvir, func_name = name, body = cbor.dumps(inputs))

            for ie in mvir.index(n_inputs.node_id()):
                if ie.kind == 'workflow_step' and ie.key == 'inputs':
                    n = mvir.node(ie.node_id)
                    if n_step is None or n.timestamp > n_step.timestamp:
                        n_step = n

        if n_step is not None:
            print(f'use workflow cache: {n_inputs.node_id()} -> {n_step.node_id()}')
            result = mvir.node(n_step.output)
        else:
            self._step_depth += 1
            try:
                result = f(self, *args, **kwargs)
            finally:
                self._step_depth -= 1

            if can_cache:
                # Create a cached node for this step, for future use.
                n_step = WorkflowStepNode.new(
                    mvir,
                    inputs = n_inputs.node_id(),
                    output = result.node_id(),
                    timestamp = datetime.now(),
                )
                mvir.set_tag('workflow_cache', n_step, name)
                print(f'save workflow cache: {n_inputs.node_id()} -> {n_step.node_id()}')

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
    def cc_cmake(self, c_code: TreeNode) -> FileNode:
        n_op_cc = self.cc_cmake_op(c_code)
        compile_commands = self.mvir.node(n_op_cc.compile_commands)
        return compile_commands

    @step
    def cc_cmake_op(self, c_code: TreeNode) -> CompileCommandsOpNode:
        return analysis.cc_cmake(self.cfg, self.mvir, c_code)

    @step
    def cc_custom(self, c_code: TreeNode, artifact: str | int | None = None) -> FileNode:
        n_op_cc = self.cc_custom_op(c_code, artifact = artifact)
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
                    .replace('ptr::from_exposed_addr', 'ptr::with_exposed_provenance')
                    .replace('.expose_addr()', '.expose_provenance()'))
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
                # being unable to remove trailing whitespace.  We log the error
                # in that case and then suppress it.
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
    ) -> tuple[TreeNode, TreeNode]:
        cfg, mvir = self.cfg, self.mvir
        cargo_dir = cfg.relative_path(cfg.transpile.output_dir)

        if provide_test_cmd and cfg.test_command is not None:
            after_refactoring_instruction = AGENT_AFTER_REFACTORING_RUN_TESTS \
                    .format(test_cmd = cfg.test_command)
        else:
            after_refactoring_instruction = AGENT_AFTER_REFACTORING_BUILD

        extra_code = [
            self.find_unsafe2_json(n_code),
        ]
        if n_test_code is not None:
            extra_code.append(n_test_code)

        prompt = AGENT_SAFETY_PROMPT.format(
            cargo_dir_path = cargo_dir,
            after_refactoring_instruction = after_refactoring_instruction,
            target_goal = target_goal.prompt(),
        )
        if prompt_suffix is not None:
            prompt = f'{prompt}\n\n{prompt_suffix}'
        return agent.run_rewrite(cfg, mvir, prompt, self.cfg.models.agent_loop, n_code,
            extra_code = extra_code,
            planning_files = n_plans,
            codex_login=self.codex_login,
            clean_cmds = [
                ['cargo', 'clean', '--manifest-path', os.path.join(cargo_dir, 'Cargo.toml')],
            ],
            find_unsafe2_json_dir = analysis.UNSAFE_JSON_DIR,
        )

    @step
    def agent_safety_no_tests(
        self,
        n_code: TreeNode,
        n_plans: TreeNode,
    ) -> tuple[TreeNode, TreeNode]:
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
    ) -> tuple[TreeNode | None, TreeNode | None]:
        cfg, mvir = self.cfg, self.mvir
        cargo_dir = cfg.relative_path(cfg.transpile.output_dir)

        if cfg.test_command is not None:
            after_refactoring_instruction = AGENT_AFTER_REFACTORING_RUN_TESTS \
                    .format(test_cmd = cfg.test_command)
        else:
            after_refactoring_instruction = AGENT_AFTER_REFACTORING_BUILD

        extra_code = [
            self.find_unsafe2_json(n_code),
        ]
        if n_test_code is not None:
            # In planning mode, we do provide the entire C code to the planner
            # since that might increase the quality of the plan. However, if
            # we later run the safety loop in `agent_sim_no_tests` mode the
            # safety agent will not have access to the C code, and the plan
            # might contain references to it. This might confuse the agent and
            # break the `agent_sim_no_tests` mode.
            # TODO: un-break that mode somehow (without hiding the C code).
            extra_code.append(n_test_code)

        prompt = AGENT_PLAN_PROMPT.format(
            cargo_dir_path = cargo_dir,
            after_refactoring_instruction = after_refactoring_instruction,
        )
        return agent.run_rewrite(cfg, mvir, prompt, self.cfg.models.agent_plan, n_code,
            extra_code = extra_code,
            planning_files = None,
            codex_login=self.codex_login,
            clean_cmds = [
                ['cargo', 'clean', '--manifest-path', os.path.join(cargo_dir, 'Cargo.toml')],
            ],
            find_unsafe2_json_dir = analysis.UNSAFE_JSON_DIR,
        )

    @step
    def do_safety_step_agent(
        self,
        n_code: TreeNode,
        n_test_code: TreeNode,
        n_plans: TreeNode,
        prompt_suffix: str | None = None,
        target_goal: AgentTarget = AgentTargetOther(),
    ) -> tuple[TreeNode | None, TreeNode | None]:
        self.fuel.use()

        n_new_code, n_plans = self.agent_safety(n_code, n_test_code, n_plans,
            prompt_suffix = prompt_suffix,
            target_goal = target_goal)
        # The change must pass tests, and must not regress any unsafe count.
        n_op_test = self.test_op(n_new_code, n_test_code)
        n_op_unsafe = self.compare_unsafe2_op(n_code, n_new_code)
        if n_op_test.exit_code == 0 and n_op_unsafe.exit_code == 0:
            return n_new_code, n_plans
        else:
            return None, None

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
        n_new_code, n_plans = self.agent_safety_no_tests(n_code, n_plans)
        n_op_check = self.cargo_check_json_op(n_new_code)
        n_op_unsafe = self.compare_unsafe2_op(n_code, n_new_code)
        if not (n_op_check.passed and n_op_unsafe.exit_code == 0):
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
