import functools
import inspect
import os
import subprocess
import sys
import toml
from typing import Any, Callable

from . import analysis, llm
from .analysis import COMPILE_COMMANDS_PATH
from .config import Config
from .mvir import (
    MVIR, Node, FileNode, TreeNode, CompileCommandsOpNode, TranspileOpNode,
    LlmOpNode, TestResultNode, FindUnsafeAnalysisNode, SplitFfiOpNode,
    CargoCheckJsonAnalysisNode, EditOpNode, SplitOpNode, MergeOpNode,
    CrateNode, DefNode,
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

LLM_PROMPT_RENAME_IDIOMATIC = '''
This Rust code was auto-translated from C, so it uses unidiomatic names in some places.  Please find these cases and rename them according to Rust conventions: snake_case for functions and variables, CamelCase for types, and SNAKE_CASE for consts and statics.  Don't change anything but the names.

{output_instructions}

{input_files}
'''

LLM_PROMPT_EXTRACT_SIGS = '''
Please extract function signatures (and only function signatures) from each
input file.  For each one, output a file with the same name, but with all
function bodies replaced with `/* ... */` and all non-function items removed.

{output_instructions}

Example input:

<file name="example.rs">
use core::ffi::c_int;
type MyInt = c_int;
fn f(x: c_int) -> c_int {{
    x + 10
}}
fn g(a: &c_int, b: &mut c_int) {{
    *b += a;
}}
</file>

Example output:

<file name="example.rs">
fn f(x: c_int) -> c_int {{ /* ... */ }}
fn g(a: &c_int, b: &mut c_int) {{ /* ... */ }}
</file>

Actual input:

{input_files}
'''


_CRISP_DIR = os.path.dirname(os.path.dirname(__file__))


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
        if self._step_depth == 0:
            print(' ** ' + name)
            bound = sig.bind(self, *args, **kwargs)
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
    def __init__(self, cfg: Config, mvir: MVIR):
        self.cfg = cfg
        self.mvir = mvir
        self._step_depth = 0

    def accept(self, code: TreeNode, reason = None):
        self.mvir.set_tag('current', code.node_id(), reason)

    @step
    def cc_cmake(self, c_code: TreeNode) -> FileNode:
        n_op_cc = self.cc_cmake_op(c_code)
        compile_commands = self.mvir.node(n_op_cc.compile_commands)
        return compile_commands

    @step
    def cc_cmake_op(self, c_code: TreeNode) -> CompileCommandsOpNode:
        return analysis.cc_cmake(self.cfg, self.mvir, c_code)

    @step
    def transpile(
        self,
        c_code: TreeNode,
        src_loc_annotations: bool = False,
        refactor_transforms: tuple[str, ...] = (),
        hayroll: bool = False,
    ) -> TreeNode:
        compile_commands = self.cc_cmake(c_code)
        n_op_transpile = self.transpile_cc_op(
            c_code,
            compile_commands,
            src_loc_annotations=src_loc_annotations,
            refactor_transforms=refactor_transforms,
            hayroll=hayroll,
        )
        if n_op_transpile.rust_code is None:
            print('error: transpile failed', file=sys.stderr)
            return None
        code = self.mvir.node(n_op_transpile.rust_code)

        # Patch Cargo.toml before building and testing.  This makes sure we
        # test the code that will actually be used, and also gives
        # `patch_cargo_toml` a chance to fix the c2rust-bitflags dependency if
        # needed.
        code = self.patch_cargo_toml(code)

        # Hack: add -lcrypto, which is required for one test case
        code = self.patch_build_rs(code, libs = ['crypto'])

        if not self.test(code, c_code):
            print('error: tests failed after transpile')
            return None
        return code

    @step
    def transpile_cc_op(
        self,
        n_c_code: TreeNode,
        n_cc: FileNode,
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
        with run_sandbox(cfg, mvir) as sb:
            output_path = cfg.relative_path(cfg.transpile.output_dir)

            sb.checkout_file(COMPILE_COMMANDS_PATH, n_cc)
            sb.checkout(n_c_code)

            # Hack: ensure all directories mentioned in compile_commands.json
            # exist by placing an empty file in each one.
            j = n_cc.body_json()
            n_empty = FileNode.new(mvir, '')
            sb_dir = sb.join()
            for x in j:
                if 'directory' in x:
                    d = x['directory']
                    rel_d = os.path.relpath(d, sb_dir)
                    sb.checkout_file(os.path.join(rel_d, '.empty'), n_empty)

            # Run c2rust-transpile
            if not hayroll:
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
                if cfg.transpile.bin_main is not None:
                    c2rust_cmd.extend((
                        '--binary', cfg.transpile.bin_main,
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
                c_path_rel = cfg.relative_path(cfg.transpile.cmake_src_dir)

                # Setting `--project-dir` explicitly prevents Hayroll from
                # including various ancestor directories as intermediate
                # modules.  We want it to translate `src/lib.c` to `src/lib.rs`
                # rather than `foo/bar/baz/src/lib.rs` because overly long file
                # paths sometimes confuse weaker LLMs.
                c2rust_cmd = [
                        'hayroll',
                        sb.join(COMPILE_COMMANDS_PATH),
                        sb.join(output_path),
                        '--project-dir', os.path.join(c_path_rel, 'src'),
                        ]
                # hayroll already has c2rust-transpile emit src loc annotations.
                if cfg.transpile.bin_main is not None:
                    c2rust_cmd.extend((
                        '--binary', cfg.transpile.bin_main,
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
    def patch_cargo_toml(self, code: TreeNode) -> TreeNode:
        n_op = self.patch_cargo_toml_op(code)
        new_code = self.mvir.node(n_op.new_code)
        return new_code

    @step
    def patch_cargo_toml_op(self, code: TreeNode) -> EditOpNode:
        cfg, mvir = self.cfg, self.mvir

        cargo_toml_paths = [k for k in code.files.keys()
                if os.path.basename(k) == 'Cargo.toml']
        assert len(cargo_toml_paths) == 1, (
                f'expected only 1 Cargo.toml in transpiler output, but got {cargo_toml_paths}')
        cargo_toml_path, = cargo_toml_paths
        cargo_toml = mvir.node(code.files[cargo_toml_path])

        t = toml.loads(cargo_toml.body_str())

        if 'bin' in t:
            kind = 'bin'
            assert isinstance(t['bin'], list)
            assert len(t['bin']) == 1
            t['bin'][0]['name'] = cfg.project_name
        else:
            kind = 'lib'
            t['package']['name'] = cfg.project_name
            t['lib']['name'] = cfg.project_name
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
    def test(self, code: TreeNode, c_code: TreeNode) -> bool:
        n = self.test_op(code, c_code)
        return n.exit_code == 0

    @step
    def test_op(self, code: TreeNode, c_code: TreeNode) -> TestResultNode:
        n = analysis.run_tests(self.cfg, self.mvir, code, c_code, self.cfg.test_command)
        return n

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
            len(file_info['internal_unsafe_fns']) + len(file_info['fns_containing_unsafe'])
            for file_info in j_unsafe.values())
        print('%d unsafe functions remaining' % unsafe_count)
        return unsafe_count

    @step
    def find_unsafe_op(self, n_code: TreeNode) -> FindUnsafeAnalysisNode:
        return analysis.find_unsafe(self.cfg, self.mvir, n_code)

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
                logs = b'\n\n'.join((logs, logs2))

            if exit_code == 0:
                exit_code, logs2 = sb.run(['rm', '-rfv', sb.join(rust_path_rel, 'target')])
                logs = b'\n\n'.join((logs, logs2))

            if exit_code == 0:
                n_new_tree = sb.commit_dir(rust_path_rel)
            else:
                # TODO: record failure without throwing an exception, like
                # `transpile_cc_op` does
                raise ValueError(
                    f'split_ffi_entry_points failed (exit code = {exit_code})\n'
                    f'logs:\n{logs.decode("utf-8", errors="replace")}')

        n_op = SplitFfiOpNode.new(
                mvir,
                old_code = n_tree.node_id(),
                new_code = n_new_tree.node_id(),
                commit = '',
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

    def _filter_defs(self, code: TreeNode, f: Callable[[str], bool]) -> CrateNode:
        mvir = self.mvir

        crate = self.split(code)

        # TODO: update merge_rust to delete defs that are absent from the json
        # input, then change this to simply filter out defs instead of
        # replacing them with `empty_def`
        empty_def = DefNode.new(mvir, b'')
        crate_erased = CrateNode.new(mvir,
            defs = {k: v if f(k) else empty_def.node_id() for k, v in crate.defs.items()})
        return crate_erased

    @step
    def erase_ffi(self, code: TreeNode) -> TreeNode:
        """
        Erase all function definitions named `*_ffi` from `code`.  They can be
        handled separately and re-inserted using `unerase_ffi`.
        """
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
        defs_out.update((k, v) for k,v in crate_ffi.defs.items()
            if k.endswith('_ffi'))
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

        code = self.unerase_ffi(orig_code, new_main_code, new_ffi_defs)
        return code

    @step
    def rename_idiomatic(self, code: TreeNode) -> TreeNode:
        cfg, mvir = self.cfg, self.mvir

        # TODO: replace with a symbolic tool.  Use suggested names from
        # `bad_style` lints: see `compiler/rustc_lint/src/nonstandard_style.rs`

        code, _llm_op = llm.run_rewrite(
                cfg, mvir, LLM_PROMPT_RENAME_IDIOMATIC, code,
                glob_filter = cfg.src_globs)

        n_op_check = self.cargo_check_json_op(code)
        if not n_op_check.passed:
            raise ValueError('rename_idiomatic: llm introduced a compile error')

        return code

    @step
    def extract_sigs(self, code: TreeNode) -> CrateNode:
        cfg, mvir = self.cfg, self.mvir

        # TODO: replace with a symbolic tool

        sigs_code, _llm_op = llm.run_rewrite(
                cfg, mvir, LLM_PROMPT_EXTRACT_SIGS, code,
                glob_filter = cfg.src_globs)

        sigs_crate = self.split(sigs_code)

        return sigs_crate

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

        # `new_ffi_tree` has a flat module structure; all FFI functions are at
        # top level.  We need to move these back to their respective paths.
        new_ffi_crate_renamed = self.split(new_ffi_tree, root_file = 'ffi.rs')
        print(new_ffi_crate_renamed.defs)
        new_ffi_defs = CrateNode.new(mvir,
            defs = {k: new_ffi_crate_renamed.defs[k.rpartition('::')[2]]
                for k in ffi_defs.defs.keys() if k.endswith('_ffi')})

        return new_ffi_defs
