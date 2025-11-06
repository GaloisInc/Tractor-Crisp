from . import analysis, llm
from .analysis import COMPILE_COMMANDS_PATH
from .config import Config
from .mvir import MVIR, FileNode, TreeNode, TranspileOpNode, TestResultNode
from .sandbox import run_sandbox


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


class Workflow:
    def __init__(self, cfg: Config, mvir: MVIR):
        self.cfg = cfg
        self.mvir = mvir

    def accept(self, code: TreeNode, reason = None):
        self.mvir.set_tag('current', code.node_id(), reason)

    def transpile(self, c_code: TreeNode) -> TreeNode:
        print(' ** cc_cmake')
        n_op_cc = analysis.cc_cmake(self.cfg, self.mvir, c_code)
        compile_commands = self.mvir.node(n_op_cc.compile_commands)

        print(' ** transpile')
        n_op_transpile = self.transpile_cc_op(c_code, compile_commands)
        code = self.mvir.node(n_op_transpile.rust_code)

        if not self.test(code, c_code):
            print('error: tests failed after transpile')
            return None
        return code

    def transpile_cc_op(self, n_c_code: TreeNode, n_cc: FileNode) -> TranspileOpNode:
        cfg, mvir = self.cfg, self.mvir
        with run_sandbox(cfg, mvir) as sb:
            output_path = cfg.relative_path(cfg.transpile.output_dir)

            sb.checkout_file(COMPILE_COMMANDS_PATH, n_cc)
            sb.checkout(n_c_code)

            # Run c2rust-transpile
            c2rust_cmd = [
                    'c2rust-transpile',
                    sb.join(COMPILE_COMMANDS_PATH),
                    '--output-dir', sb.join(output_path),
                    '--emit-build-files',
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

    def test(self, code: TreeNode, c_code: TreeNode) -> bool:
        n = self.test_op(code, c_code)
        return n.exit_code == 0

    def test_op(self, code: TreeNode, c_code: TreeNode) -> TestResultNode:
        print(' ** test')
        print('code = %s' % code.node_id())
        print('c_code = %s' % c_code.node_id())
        n = analysis.run_tests(self.cfg, self.mvir, code, c_code, self.cfg.test_command)
        print('test exit code = %d' % n.exit_code)
        return n

    def count_unsafe(self, n_code: TreeNode) -> int:
        print(' ** count_unsafe')
        print('code = %s' % n_code.node_id())
        n_find_unsafe = analysis.find_unsafe(self.cfg, self.mvir, n_code)
        j_unsafe = n_find_unsafe.body_json()
        unsafe_count = sum(
            len(file_info['internal_unsafe_fns']) + len(file_info['fns_containing_unsafe'])
            for file_info in j_unsafe.values())
        print('%d unsafe functions remaining' % unsafe_count)
        return unsafe_count

    def llm_safety(self, n_code: TreeNode) -> TreeNode:
        print(' ** llm (safety)')
        print('code = %s' % n_code.node_id())
        n_new_code, n_op_llm = llm.run_rewrite(
                self.cfg, self.mvir, LLM_SAFETY_PROMPT, n_code,
                glob_filter = self.cfg.src_globs)
        print('new code = %s' % n_new_code.node_id())
        return n_new_code

    def llm_repair(self, n_code: TreeNode, n_op_test: TestResultNode) -> TreeNode:
        print(' ** llm (repair)')
        print('code = %s' % n_code.node_id())
        n_new_code, n_op_llm = llm.run_rewrite(
                self.cfg, self.mvir, LLM_REPAIR_PROMPT, n_code,
                glob_filter = self.cfg.src_globs,
                format_kwargs = {'test_output': n_op_test.body_str()},
                think = True)
        print('new code = %s' % n_new_code.node_id())
        return n_new_code

