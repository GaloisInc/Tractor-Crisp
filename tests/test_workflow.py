import contextlib
import io
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
import toml

from crisp.config import Config, TranspileConfig
from crisp.error import CrispError
from crisp.mvir import MVIR, FileNode, TreeNode
from crisp.planning import StepPlan, StepGoal
from crisp.workflow import (
    FFI_SEEN_FINDINGS_CAP, Workflow, merge_ffi_finding_titles,
    upgrade_toolchain_rust_src,
)


class AgentGenTestsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.mvir = MVIR(directory.name, '.')

        def tree(body):
            return TreeNode.new(self.mvir, files={
                'rust/lib.rs': FileNode.new(self.mvir, body).node_id()})

        self.original, self.current = map(tree, ('original', 'current'))
        self.c_code = TreeNode.new(self.mvir, files={})
        self.candidate = TreeNode.new(self.mvir, files={**self.current.files,
            'crisp_tests/tests/parse.c': FileNode.new(self.mvir, '// RUN: test_parse').node_id()})
        cfg = Config(config_path='crisp.toml', project_name='example', src_globs=[],
            transpile=TranspileConfig(config_path='crisp.toml', output_dir='rust', artifacts=[]))
        self.w = Workflow(cfg, self.mvir)
        detail = FileNode.new(self.mvir, 'Preserve parser buffer behavior')
        self.plan = StepPlan('Make parser safe', goals=[
            StepGoal('total_unsafe', functions=['api::parse']),
            StepGoal('field_contains_raw_ptr', types=['Buffer']),
        ], details=[detail.node_id()])
        unsafe = [{
            'fns': {'api::parse': {'ffi_symbol': None, 'filename': 'src/lib.rs',
                'total_unsafe': 3}},
            'types': {'Buffer': {'filename': 'src/lib.rs', 'field_contains_raw_ptr': 1}},
        }]
        self.w.find_unsafe2_json_dicts = lambda code: unsafe
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_generator_targets_plan_and_validates_against_original_reference(self):
        op = SimpleNamespace(exit_code=0, passed=True, body=lambda: b'passed\n')

        def rewrite(*args, **kwargs):
            self.assertIs(args[4], self.current)
            self.assertEqual(kwargs['extra_code'], {'c_code': self.c_code})
            prompt = args[2]
            for text in ('Make parser safe', 'api::parse', 'Buffer', 'src/lib.rs',
                         'Preserve parser buffer behavior', 'crisp_run_tests.sh'):
                self.assertIn(text, prompt)
            app = FastAPI()
            kwargs['http_build_app'](SimpleNamespace(get_output=lambda name: self.candidate), app)
            route = next(r for r in app.routes if r.path == '/crisp/run_tests')
            self.assertEqual(route.endpoint().status_code, 200)
            return self.candidate, None

        for reference in (self.original, None):
            with self.subTest(explicit_reference=reference is not None), \
                    patch('crisp.workflow.agent.run_rewrite', side_effect=rewrite), \
                    patch.object(self.w, 'test_op', return_value=op) as test, \
                    patch.object(self.w, 'compare_observation_tests_op', return_value=op) as compare:
                result = self.w.agent_gen_tests(self.current, self.c_code, self.plan,
                    n_orig_code=reference)
                self.assertIs(result, self.candidate)
                test.assert_called_once_with(self.candidate, self.c_code)
                self.assertEqual(compare.call_count, 2)
                for args in compare.call_args_list:
                    self.assertEqual(args.args, (
                        self.original if reference is not None else self.current,
                        self.candidate, self.c_code))

    def test_generator_rejects_missing_output_implementation_changes_and_invalid_tests(self):
        for candidate, message, checked in (
                (None, 'failed to produce any code', False),
                (self.original, 'modified files outside crisp_tests/', False),
                (self.candidate, 'generated differential tests failed', True)):
            with self.subTest(message=message), \
                    patch('crisp.workflow.agent.run_rewrite', return_value=(candidate, None)), \
                    patch.object(self.w, 'compare_observation_tests_op',
                        return_value=SimpleNamespace(exit_code=1)) as compare:
                with self.assertRaisesRegex(CrispError, message):
                    self.w.agent_gen_tests(self.current, self.c_code, self.plan)
                self.assertEqual(compare.call_count, int(checked))


class AgentObservationReferenceTest(unittest.TestCase):
    def test_test_endpoint_uses_original_reference_and_defaults_to_input_code(self):
        with tempfile.TemporaryDirectory() as storage:
            mvir = MVIR(storage, '.')

            def tree(body):
                return TreeNode.new(mvir, files={
                    'rust/lib.rs': FileNode.new(mvir, body).node_id()})

            original, current, candidate = map(tree, ('original', 'current', 'candidate'))
            current = TreeNode.new(mvir, files={**current.files,
                'crisp_tests/tests/api.py': FileNode.new(mvir, 'print("observation")').node_id()})
            c_code = TreeNode.new(mvir, files={})
            # Generated differential tests must be offered even without a
            # pre-existing C project test command.
            cfg = Config(config_path='crisp.toml', project_name='example', src_globs=[],
                transpile=TranspileConfig(
                    config_path='crisp.toml', output_dir='rust', artifacts=[]))
            w = Workflow(cfg, mvir)
            op = SimpleNamespace(passed=True, body=lambda: b'passed\n')

            def rewrite(*args, **kwargs):
                self.assertIn('crisp_run_tests.sh', args[2])
                app = FastAPI()
                asb = SimpleNamespace(get_output=lambda name: candidate)
                kwargs['http_build_app'](asb, app)
                route = next(r for r in app.routes if r.path == '/crisp/run_tests')
                response = route.endpoint()
                self.assertEqual(response.status_code, 200)
                self.assertIn(b'Part 1', response.body)
                self.assertIn(b'Part 2', response.body)
                return candidate, None

            for reference in (original, None):
                with self.subTest(explicit_reference=reference is not None), \
                        patch('crisp.workflow.agent.run_rewrite', side_effect=rewrite), \
                        patch.object(w, 'find_unsafe2_json', return_value=c_code) as unsafe, \
                        patch.object(w, 'test_op', return_value=op) as test, \
                        patch.object(w, 'compare_observation_tests_op', return_value=op) as compare, \
                        contextlib.redirect_stdout(io.StringIO()):
                    w.agent_safety(current, c_code, None, n_orig_code=reference)
                    compare.assert_called_once_with(
                        original if reference is not None else current, candidate, c_code)
                    test.assert_called_once_with(candidate, c_code)
                    unsafe.assert_called_once_with(current)


# Finding lines as rendered by `codex exec review` (from a real zlib run).
REPORT = '''
The diff removes `unsafe` from several exported entry points.

- [P1] Restore `unsafe` on `gz_intmax_ffi` — /root/work/translated_rust/src/gzlib.rs:1425-1425
- [P1] Restore `unsafe` on `zlibVersion_ffi` — /root/work/translated_rust/src/zutil.rs:27-27
- [P2] Wrapper contains validation logic — /root/work/translated_rust/src/gzlib.rs:100-120
'''


class MergeFfiFindingTitlesTest(unittest.TestCase):
    def test_extracts_titles_without_locations(self):
        self.assertEqual(merge_ffi_finding_titles([], REPORT), [
            'Restore `unsafe` on `gz_intmax_ffi`',
            'Restore `unsafe` on `zlibVersion_ffi`',
            'Wrapper contains validation logic',
        ])

    def test_merge_deduplicates(self):
        seen = merge_ffi_finding_titles([], REPORT)
        self.assertEqual(merge_ffi_finding_titles(list(seen), REPORT), seen)

    def test_bounded_keeps_most_recent(self):
        report = '\n'.join(
            f'- [P1] finding {i} — src/a.rs:{i}-{i}' for i in range(20))
        seen = merge_ffi_finding_titles([], report)
        self.assertEqual(len(seen), FFI_SEEN_FINDINGS_CAP)
        self.assertEqual(seen[-1], 'finding 19')

    def test_clean_report_adds_nothing(self):
        self.assertEqual(merge_ffi_finding_titles([], 'No violations found.'), [])


# Variadic definitions as emitted by c2rust 0.22.1 (edition 2021) for `...`
# functions, `va_list` parameters, `va_copy`, `va_list *` and a `va_list`
# struct member.
VA_RS = '''
#![feature(c_variadic)]
#![feature(raw_ref_op)]
#[repr(C)]
pub struct S<'a> {
    pub ap: ::core::ffi::VaListImpl<'a>,
    pub k: ::core::ffi::c_int,
}
#[no_mangle]
pub unsafe extern "C" fn vnext(mut ap: *mut ::core::ffi::VaListImpl) -> ::core::ffi::c_int {
    return (*ap).arg::<::core::ffi::c_int>();
}
#[no_mangle]
pub unsafe extern "C" fn fwd(
    mut buf: *mut ::core::ffi::c_char,
    mut fmt: *const ::core::ffi::c_char,
    mut c2rust_args: ...
) -> ::core::ffi::c_int {
    let mut ap: ::core::ffi::VaListImpl;
    ap = c2rust_args.clone();
    let mut r: ::core::ffi::c_int = vsprintf(buf, fmt, ap.as_va_list());
    return r;
}
#[no_mangle]
pub unsafe extern "C" fn vsum(
    mut n: ::core::ffi::c_int,
    mut ap: ::core::ffi::VaList,
) -> ::core::ffi::c_int {
    let mut s: ::core::ffi::c_int = 0 as ::core::ffi::c_int;
    let mut i: ::core::ffi::c_int = 0 as ::core::ffi::c_int;
    while i < n {
        s += ap.arg::<::core::ffi::c_int>();
        i += 1;
    }
    return s;
}
#[no_mangle]
pub unsafe extern "C" fn sum2(
    mut n: ::core::ffi::c_int,
    mut c2rust_args: ...
) -> ::core::ffi::c_int {
    let mut ap: ::core::ffi::VaListImpl;
    let mut aq: ::core::ffi::VaListImpl;
    ap = c2rust_args.clone();
    aq = ap.clone();
    let mut s: ::core::ffi::c_int = vsum(n, ap.as_va_list()) + vsum(n, aq.as_va_list());
    return s;
}
'''


class UpgradeToolchainRustSrcTest(unittest.TestCase):
    def test_rewrites_va_list_api(self):
        out = upgrade_toolchain_rust_src(VA_RS)
        for old in ('VaListImpl', 'as_va_list', '.arg::<', 'feature(c_variadic)'):
            self.assertNotIn(old, out)
        for new in (
            "pub ap: ::core::ffi::VaList<'a>,",
            'fn vnext(mut ap: *mut ::core::ffi::VaList) ->',
            'return (*ap).next_arg::<::core::ffi::c_int>();',
            'let mut ap: ::core::ffi::VaList;',
            'vsprintf(buf, fmt, ap.clone());',
            's += ap.next_arg::<::core::ffi::c_int>();',
            'vsum(n, ap.clone()) + vsum(n, aq.clone());',
        ):
            self.assertIn(new, out)

    def test_idempotent(self):
        out = upgrade_toolchain_rust_src(VA_RS)
        self.assertEqual(upgrade_toolchain_rust_src(out), out)

    def test_rewrites_strict_provenance_api(self):
        src = (
            '#![feature(raw_ref_op)]\n'
            'let p = ::core::ptr::from_exposed_addr_mut::<T>(q.expose_addr());\n'
            'let r = ::core::ptr::from_exposed_addr::<T>(a);\n'
        )
        self.assertEqual(upgrade_toolchain_rust_src(src), (
            '\n'
            'let p = ::core::ptr::with_exposed_provenance_mut::<T>(q.expose_provenance());\n'
            'let r = ::core::ptr::with_exposed_provenance::<T>(a);\n'
        ))

    def test_leaves_other_code_alone(self):
        src = (
            'pub type MyVaListImpl2 = ::core::ffi::c_int;\n'
            'let mut ap: ::core::ffi::VaList = c2rust_args.clone();\n'
            'cmd.arg(x);\n'
            '(*s).arg = (*s).arg + 1;\n'
        )
        self.assertEqual(upgrade_toolchain_rust_src(src), src)


class PatchBuildRsTest(unittest.TestCase):
    def patch_build_rs(self, paths: dict[str, str], libs: list[str]):
        """
        Run `patch_build_rs` on a tree with the given file contents.  Returns
        the old and new trees, and a function for reading a file of the latter.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mvir = MVIR(tmp.name, '.')
        code = TreeNode.new(mvir, files = {
            path: FileNode.new(mvir, src).node_id() for path, src in paths.items()})
        # Steps print a trace of their arguments and results.
        with contextlib.redirect_stdout(io.StringIO()):
            new_code = Workflow(None, mvir).patch_build_rs(code, libs = libs)
        return code, new_code, lambda path: mvir.node(new_code.files[path]).body_str()

    def test_writes_link_libs(self):
        code, new_code, read = self.patch_build_rs({
            'translated_rust/Cargo.toml': '[package]\n',
            'translated_rust/build.rs': 'fn main() {}\n',
            'translated_rust/src/shell.rs': 'pub fn main_0() {}\n',
        }, ['m', 'z'])
        self.assertEqual(read('translated_rust/build.rs'), (
            'fn main() {\n'
            '    println!("cargo:rustc-link-lib=m");\n'
            '    println!("cargo:rustc-link-lib=z");\n'
            '}\n'
        ))
        self.assertEqual(set(new_code.files), set(code.files))

    def test_ignores_transpiled_build_c(self):
        # SQLite has a `src/build.c`, which c2rust transpiles to `src/build.rs`.
        code, new_code, read = self.patch_build_rs({
            'translated_rust/Cargo.toml': '[package]\n',
            'translated_rust/build.rs': 'fn main() {}\n',
            'translated_rust/src/build.rs': 'pub fn sqlite3StartTable() {}\n',
        }, ['m'])
        self.assertIn('rustc-link-lib=m', read('translated_rust/build.rs'))
        self.assertEqual(
            new_code.files['translated_rust/src/build.rs'],
            code.files['translated_rust/src/build.rs'])


class PatchCargoTomlWorkspaceTest(unittest.TestCase):
    def profiles(self, paths: dict[str, str]) -> dict:
        """
        Run `patch_cargo_toml_workspace` on a tree with the given file contents,
        with `translated_rust` as the workspace root.  Returns the root
        manifest's profiles.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mvir = MVIR(tmp.name, '.')
        cfg = types.SimpleNamespace(
            transpile = types.SimpleNamespace(output_dir = 'translated_rust'),
            relative_path = lambda path: path,
        )
        code = TreeNode.new(mvir, files = {
            path: FileNode.new(mvir, src).node_id() for path, src in paths.items()})
        with contextlib.redirect_stdout(io.StringIO()):
            new_code = Workflow(cfg, mvir).patch_cargo_toml_workspace(code)
        root = mvir.node(new_code.files['translated_rust/Cargo.toml']).body_str()
        return toml.loads(root)['profile']

    def test_single_codegen_unit_in_root_manifest(self):
        self.assertEqual(self.profiles({
            'translated_rust/Cargo.toml':
                '[package]\nname = "a"\n'
                '[profile.dev]\nopt-level = 1\ncodegen-units = 16\n'
                '[profile.release]\nstrip = "debuginfo"\n',
            'translated_rust/b/Cargo.toml': '[package]\nname = "b"\n',
        }), {
            'dev': {'opt-level': 1, 'codegen-units': 1},
            'release': {'strip': 'debuginfo', 'codegen-units': 1},
        })

    def test_single_codegen_unit_in_virtual_manifest(self):
        self.assertEqual(self.profiles({
            'translated_rust/b/Cargo.toml': '[package]\nname = "b"\n',
        }), {
            'dev': {'codegen-units': 1},
            'release': {'codegen-units': 1},
        })
