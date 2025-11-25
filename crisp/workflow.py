import functools
import inspect
import os
import subprocess
import sys
from typing import Any

from . import analysis, llm
from .analysis import COMPILE_COMMANDS_PATH
from .config import Config
from .mvir import MVIR, Node, FileNode, TreeNode, CompileCommandsOpNode, \
        TranspileOpNode, LlmOpNode, TestResultNode, FindUnsafeAnalysisNode, \
        SplitFfiOpNode, CargoCheckJsonAnalysisNode
from .sandbox import run_sandbox
from .work_dir import lock_work_dir


LLM_SAFETY_PROMPT = '''
This Rust code was auto-translated from C, so it is partly unsafe. Your task is to convert it to safe Rust, without changing its behavior. You must replace all unsafe operations (such as raw pointer dereferences and libc calls) with safe ones, so that you can remove unsafe blocks from the code and convert unsafe functions to safe ones. You may adjust types and data structures (such as replacing raw pointers with safe references) as needed to accomplish this.

HOWEVER, any function marked #[no_mangle] is an FFI entry point, which means its signature must not be changed. If such a function has unsafe types (such as raw pointers) in its signature and contains nontrivial logic, you should handle it as follows:
1. For a #[no_mangle] entry point named `foo`, make a new function `foo_impl` without the #[no_mangle] attribute.
2. Move all the logic from `foo` into `foo_impl`, and have `foo` simply be a wrapper that calls `foo_impl`.
3. If there are any calls to `foo` in the Rust code, change them to call `foo_impl` instead.
You can then make `foo_impl` safe like any other function, leaving `foo` as a simple unsafe wrapper for FFI callers.

After making the code safe, output the updated Rust code in a Markdown code block, with the file path on the preceding line, as shown in the input.

{input_files}
'''

LLM_REPAIR_PROMPT = '''
I tried compiling this Rust code and running the tests, but I got an error. Please fix the error so the code compiles and passes the tests. Try to avoid introducing any more unsafe code beyond what's already there.

Output the resulting Rust code in a Markdown code block, with the file path on the preceding line, as shown in the input.

{input_files}

Build/test logs:

```
{test_output}
```
'''

LLM_REPAIR_COMPILE_PROMPT = '''
I tried compiling this Rust code, but I got an error. Please fix the error so the code compiles.

Don't add new unsafe blocks unless absolutely necessary. If the error is due to an unsafe function call or other operation, try to replace it with an equivalent safe operation instead.

Output the resulting Rust code in a Markdown code block, with the file path on the preceding line, as shown in the input.

{input_files}

Compiler logs:

```
{stderr}
```
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
    def transpile(self, c_code: TreeNode, hayroll: bool = False) -> TreeNode:
        compile_commands = self.cc_cmake(c_code)
        n_op_transpile = self.transpile_cc_op(c_code, compile_commands, hayroll = hayroll)
        if n_op_transpile.rust_code is None:
            print('error: transpile failed', file=sys.stderr)
            return None
        code = self.mvir.node(n_op_transpile.rust_code)

        if not self.test(code, c_code):
            print('error: tests failed after transpile')
            return None
        return code

    @step
    def transpile_cc_op(
        self,
        n_c_code: TreeNode,
        n_cc: FileNode,
        hayroll: bool = False,
    ) -> TranspileOpNode:
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
                        'c2rust', 'transpile',
                        sb.join(COMPILE_COMMANDS_PATH),
                        '--output-dir', sb.join(output_path),
                        '--emit-build-files',
                        ]
                if cfg.transpile.bin_main is not None:
                    c2rust_cmd.extend((
                        '--binary', cfg.transpile.bin_main,
                        ))
                exit_code, logs = sb.run(c2rust_cmd)
            else:
                # Hacks to get the C path relative to `n_tree`.  This handles
                # tricks like `base_dir = ".."` used by the testing scripts.
                # TODO: clean up config path handling and get rid of this
                config_path = os.path.abspath(os.path.dirname(cfg.config_path))
                base_path = os.path.abspath(cfg.base_dir)
                c_path = os.path.join(config_path, cfg.transpile.cmake_src_dir)
                c_path_rel = os.path.relpath(c_path, base_path)

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
                if cfg.transpile.bin_main is not None:
                    c2rust_cmd.extend((
                        '--binary', cfg.transpile.bin_main,
                        ))
                exit_code, logs = sb.run(c2rust_cmd)

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
            print(repr(logs))
        print('c2rust process %s with code %d:\n%s' % (
            'succeeded' if n_op.exit_code == 0 else 'failed', n_op.exit_code, n_op.cmd))

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
    def llm_safety(self, n_code: TreeNode) -> TreeNode:
        n_new_code, n_op_llm = self.llm_safety_op(n_code)
        return n_new_code

    @step
    def llm_safety_op(self, n_code: TreeNode) -> tuple[TreeNode, LlmOpNode]:
        return llm.run_rewrite(
                self.cfg, self.mvir, LLM_SAFETY_PROMPT, n_code,
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
        """
        Note: the `split_ffi` tool may expand proc macros, so it should only be
        used on trusted code (i.e. not LLM output).
        """
        cfg, mvir = self.cfg, self.mvir

        split_ffi_dir = os.path.join(_CRISP_DIR, 'tools/split_ffi_entry_points')
        subprocess.run(('cargo', 'build', '--release'),
            cwd=split_ffi_dir, check=True)

        commit = analysis.crisp_git_state('tools/split_ffi_entry_points')

        # Hacks to get the transpiled Rust path relative to `n_tree`.  This handles
        # tricks like `base_dir = ".."` used by the testing scripts.
        # TODO: clean up config path handling and get rid of this
        config_path = os.path.abspath(os.path.dirname(cfg.config_path))
        base_path = os.path.abspath(cfg.base_dir)
        rust_path = os.path.join(config_path, cfg.transpile.output_dir)
        rust_path_rel = os.path.relpath(rust_path, base_path)

        with lock_work_dir(cfg, mvir) as wd:
            wd.checkout(n_tree)

            wd_rust_path = os.path.abspath(os.path.join(wd.path, rust_path_rel))
            p = subprocess.run(
                ('cargo', 'run', '--release',
                    '--manifest-path', os.path.join(split_ffi_dir, 'Cargo.toml'),
                    '--', wd_rust_path),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            if p.returncode != 0:
                print('command failed with exit code %d' % p.returncode)
                print(' --- stdout ---\n%s\n' % p.stdout.decode('utf-8', errors='replace'))
                print(' --- stderr ---\n%s\n' % p.stderr.decode('utf-8', errors='replace'))
                p.check_returncode()

            p2 = subprocess.run(('cargo', 'fmt'), cwd=wd_rust_path,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            new_files = n_tree.files.copy()
            for k in new_files:
                new_files[k] = wd.commit_file(k).node_id()
            n_new_tree = TreeNode.new(mvir, files=new_files)

        n_op = SplitFfiOpNode.new(
                mvir,
                old_code = n_tree.node_id(),
                new_code = n_new_tree.node_id(),
                commit = commit,
                body = p.stdout + b'\n\n' + p2.stdout,
                )

        return n_op
