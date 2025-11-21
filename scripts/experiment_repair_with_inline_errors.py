"""
Find the most recent test failure that was due to compile errors, and try to
repair it using a regular LLM prompt or one with inline error comments.
"""

import os

from crisp import inline_errors, llm
from crisp.config import Config
from crisp.mvir import MVIR, TreeNode, TestResultNode
from crisp.workflow import Workflow

def code_has_compile_error(w: Workflow, n_code: TreeNode) -> bool:
    check_json = w.cargo_check_json(n_code)
    for j in check_json:
        if j['reason'] == 'compiler-message' and j['message']['level'] == 'error':
            return True
        if j['reason'] == 'build-finished' and j['success'] == False:
            return True
    return False

REPAIR_NO_ERRORS_PROMPT = '''
I tried compiling this Rust code, but I got an error. Please fix the error so the code compiles. Try to avoid introducing any more unsafe code beyond what's already there.

Output the resulting Rust code in a Markdown code block, with the file path on the preceding line, as shown in the input.

{input_files}
'''

def do_repair_no_errors(cfg: Config, mvir: MVIR, n_code: TreeNode) -> TreeNode:
    w = Workflow(cfg, mvir)
    n_new_code, _n_op = llm.run_rewrite(
            cfg, mvir, REPAIR_NO_ERRORS_PROMPT, n_code,
            glob_filter = cfg.src_globs,
            think = True)
    return n_new_code

REPAIR_BASIC_PROMPT = '''
I tried compiling this Rust code, but I got an error. Please fix the error so the code compiles. Try to avoid introducing any more unsafe code beyond what's already there.

Output the resulting Rust code in a Markdown code block, with the file path on the preceding line, as shown in the input.

{input_files}

Compiler logs:

```
{stderr}
```
'''

def do_repair_basic(cfg: Config, mvir: MVIR, n_code: TreeNode) -> TreeNode:
    w = Workflow(cfg, mvir)
    json_errors = w.cargo_check_json(n_code)
    _errors_by_file, stderr_text = inline_errors.extract_diagnostics(json_errors)
    n_new_code, _n_op = llm.run_rewrite(
            cfg, mvir, REPAIR_BASIC_PROMPT, n_code,
            glob_filter = cfg.src_globs,
            format_kwargs = {'stderr': stderr_text.strip('\n')},
            think = True)
    return n_new_code

REPAIR_INLINE_PROMPT = '''
I tried compiling this Rust code, but I got an error. Please fix the error so the code compiles. I've added the error messages inline as comments for your reference. Try to avoid introducing any more unsafe code beyond what's already there.

Output the resulting Rust code in a Markdown code block, with the file path on the preceding line, as shown in the input.

{input_files}
'''

def do_repair_inline(cfg: Config, mvir: MVIR, n_code: TreeNode) -> TreeNode:
    w = Workflow(cfg, mvir)
    n_code_inline = w.inline_errors(n_code)
    n_new_code, _n_op = llm.run_rewrite(
            cfg, mvir, REPAIR_INLINE_PROMPT, n_code_inline,
            glob_filter = cfg.src_globs,
            think = True)
    return n_new_code

REPAIR_INLINE2_PROMPT = '''
You are an expert at resolving Rust errors.
Your task is to resolve the errors in the code. Each error block is marked by the //ERROR comment above it
You are provided with the following code that contains errors in an in-lined format:

{input_files}

The error messages associated with each line have been annotated and associated error messages have been provided after the relevant line of code.     
Provide the corrected code for the file(s) that need to be modified. Output the resulting Rust code in a Markdown code block, with the file path on the preceding line, as shown in the input.
'''

def do_repair_inline2(cfg: Config, mvir: MVIR, n_code: TreeNode) -> TreeNode:
    w = Workflow(cfg, mvir)
    n_code_inline = w.inline_errors(n_code)
    n_new_code, _n_op = llm.run_rewrite(
            cfg, mvir, REPAIR_INLINE2_PROMPT, n_code_inline,
            glob_filter = cfg.src_globs,
            think = True)
    return n_new_code

def main():
    cfg = Config.from_toml_file('crisp.toml')
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir)

    # Hacks to get the transpiled Rust path relative to `n_tree`.  This handles
    # tricks like `base_dir = ".."` used by the testing scripts.
    # TODO: clean up config path handling and get rid of this
    config_path = os.path.abspath(os.path.dirname(cfg.config_path))
    base_path = os.path.abspath(cfg.base_dir)
    rust_path = os.path.join(config_path, cfg.transpile.output_dir)
    rust_path_rel = os.path.relpath(rust_path, base_path)

    # Find a test run that failed due to compile errors.
    n_code_with_errors = None
    for e in reversed(mvir.tag_reflog('test_results')):
        n_test = mvir.node(e.node_id)
        if not isinstance(n_test, TestResultNode):
            continue
        if n_test.passed:
            continue

        n_code = mvir.node(n_test.code)
        if not code_has_compile_error(w, n_code):
            continue

        n_code_with_errors = n_code
        break

    if n_code_with_errors is None:
        print('no compile errors found')
        return
    n_code = n_code_with_errors

    candidates = [
            do_repair_no_errors,
            #do_repair_basic,
            #do_repair_inline,
            do_repair_inline2,
            ]
    N = 5
    success_count = [0] * len(candidates)
    for i in range(N):
        for j, f in enumerate(candidates):
            print('\n\n === %s, attempt %d ===' % (f.__name__, i + 1))
            n_new_code = f(cfg, mvir, n_code)
            ok = not code_has_compile_error(w, n_new_code)
            if ok:
                success_count[j] += 1
                print('%s: success' % f.__name__)
            else:
                print('%s: failed' % f.__name__)

    print('\n\nsuccess counts (out of %d attempts):' % N)
    for j, f in enumerate(candidates):
        print('%3d  %s' % (success_count[j], f.__name__))


if __name__ == '__main__':
    main()
