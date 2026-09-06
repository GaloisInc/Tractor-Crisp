import contextlib
import io
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from crisp import prompts
from crisp.analysis import check_unsafe2_findings, review_passed
from crisp.mvir import MVIR, FileNode, TreeNode
from crisp.workflow import (
    AGENT_SAFETY_PROMPT, FFI_SEEN_FINDINGS_CAP, Workflow,
    merge_ffi_finding_titles, upgrade_toolchain_rust_src,
)

# Finding lines as rendered by `codex exec review` (from a real zlib run).
REPORT = '''
The diff removes `unsafe` from several exported entry points.

- [P1] Restore `unsafe` on `gz_intmax_ffi` — /root/work/translated_rust/src/gzlib.rs:1425-1425
- [P1] Restore `unsafe` on `zlibVersion_ffi` — /root/work/translated_rust/src/zutil.rs:27-27
- [P2] Wrapper contains validation logic — /root/work/translated_rust/src/gzlib.rs:100-120
'''


class SafetyReviewTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.mvir = MVIR(temp.name, temp.name)
        cfg = SimpleNamespace(
            transpile=SimpleNamespace(output_dir='crate'),
            relative_path=lambda path: path,
            models=SimpleNamespace(agent_review='configured-review-model'),
        )
        self.w = Workflow(cfg, self.mvir)
        self.reference = self.tree('api.h', 'typedef void *(*alloc_fn)(void *context);')
        self.check = Mock()
        self.check.body_str.return_value = (
            'warning: unused variable: `x`\n'
            'warning: crate::f: raw pointer derefs increased: 0 -> 1')
        # Any node stands in for the agent op.
        self.review = self.enterContext(patch('crisp.analysis.agent.run_review_op',
            return_value=FileNode.new(self.mvir, 'agent op')))
        self.results = self.enterContext(patch('crisp.analysis.agent.review_op_results'))
        self.set_review('CRISP_REVIEW: PASS\nChecked contracts and callers.', True)
        self.enterContext(patch('builtins.print'))

    def tree(self, path, body):
        return TreeNode.new(self.mvir,
            files={path: FileNode.new(self.mvir, body).node_id()})

    def set_review(self, report, ran_commands):
        n_report = FileNode.new(self.mvir, report) if report else None
        self.results.return_value = n_report, b'log', ran_commands

    def test_callback_and_neutral_cleanup_changes_receive_review_without_warnings(self):
        for old, new in [
            ('type Alloc = unsafe extern "C" fn(*mut u8);',
             'type Alloc = extern "C" fn(*mut u8);'),
            ('fn cleanup() { let result = release_owner(); debug_assert!(result.is_none()); }',
             'fn cleanup() { debug_assert!(release_owner().is_none()); }'),
        ]:
            with self.subTest(new=new):
                self.review.reset_mock()
                baseline = self.tree('crate/src/lib.rs', old)
                candidate = self.tree('crate/src/lib.rs', new)
                self.assertEqual(self.w.do_safety_review(baseline, candidate,
                    self.reference, self.check), (True, None))
                self.review.assert_called_once()
                args, kwargs = self.review.call_args
                self.assertEqual(args[3], 'configured-review-model')
                self.assertEqual(args[4:6], (baseline, candidate))
                self.assertEqual(kwargs['extra_code'], {'c_code': self.reference})
                self.assertEqual(kwargs['effort'], 'xhigh')
                self.assertIn('at their original paths', args[2])
                self.assertIn('crate::f: raw pointer derefs increased', args[2])
                self.assertNotIn('unused variable', args[2])
                self.assertIn(prompts.SAFETY_REVIEW_RULES, args[2])

    def test_identical_tree_skips_model(self):
        code = self.tree('crate/src/lib.rs', 'fn example() {}')
        self.assertEqual(self.w.do_safety_review(code, code, self.reference, self.check),
            (True, None))
        self.review.assert_not_called()

    def test_missing_report_and_missing_command_evidence_reject_and_record_reason(self):
        baseline = self.tree('crate/src/lib.rs', 'fn before() {}')
        for i, (report, inspected) in enumerate([('', True), ('No findings.', True),
                ('CRISP_REVIEW: PASS', False), ('CRISP_REVIEW: FAIL\nMissing context.', True)]):
            with self.subTest(report=report, inspected=inspected):
                # A distinct candidate per case avoids the cached review.
                candidate = self.tree('crate/src/lib.rs', f'fn after{i}() {{}}')
                self.set_review(report, inspected)
                passed, reason = self.w.do_safety_review(baseline, candidate,
                    self.reference, self.check)
                self.assertFalse(passed)
                self.assertTrue(reason.startswith('CRISP_REVIEW: '))
                entry = self.mvir.tag_reflog('op_history')[-1]
                op = self.mvir.node(entry.node_id)
                self.assertFalse(op.passed)
                self.assertEqual(self.mvir.node(op.report).body_str(), reason)


class ReviewRuleParityTest(unittest.TestCase):
    def test_worker_prompt_contains_canonical_review_rules(self):
        prompt = AGENT_SAFETY_PROMPT.format(
            cargo_dir_path='translated_rust',
            after_refactoring_instruction='run tests',
            target_goal='',
            checker_rules=prompts.CHECKER_RULES,
            safety_review_rules=prompts.SAFETY_REVIEW_RULES,
            ffi_entry_point_rules=prompts.FFI_ENTRY_POINT_RULES,
        )

        self.assertIn(prompts.SAFETY_REVIEW_RULES, prompt)
        self.assertIn(prompts.FFI_ENTRY_POINT_RULES, prompt)
        self.assertIn('notes in it are guidance, not rules', prompt)

    def test_semantic_reviewer_uses_the_same_rules(self):
        prompt = prompts.AGENT_SAFETY_REVIEW.format(
            cargo_dir_path='translated_rust',
            reference_instruction='Original C is at its original paths.',
            checker_findings='(none)',
            safety_review_rules=prompts.SAFETY_REVIEW_RULES,
            ffi_entry_point_rules=prompts.FFI_ENTRY_POINT_RULES,
        )
        self.assertIn(prompts.SAFETY_REVIEW_RULES, prompt)
        self.assertIn(prompts.FFI_ENTRY_POINT_RULES, prompt)
        self.assertIn('overall_explanation', prompt)
        self.assertIn('CRISP_REVIEW: PASS', prompt)


class CheckUnsafe2FindingsTest(unittest.TestCase):
    def test_keeps_checker_findings_and_drops_rustc_lints(self):
        findings = [
            "warning: crate::src::m::f_ffi: 10 unsafe operations now inside "
            "count-exempt FFI entry point (baseline 2); entry points must stay thin",
            "warning: crate::src::m::Access::<'a, State>::read: "
            "raw pointer derefs increased: 0 -> 1",
            "crate::src::m::g: int-to-pointer casts increased: 0 -> 1",
        ]
        logs = '\n'.join([
            'warning: `sqlite3_step` redeclared with a different signature',
            '  --> src/m.rs:10:5',
            findings[0],
            'warning: unused variable: `x`',
            findings[1],
            findings[2],
            "warning: `crate` (lib) generated 2 warnings",
        ])
        n_op = Mock()
        n_op.body_str.return_value = logs

        self.assertEqual(check_unsafe2_findings(n_op), '\n'.join(findings))


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


class ReviewVerdictTest(unittest.TestCase):
    def test_completed_explicit_approval_passes(self):
        for verdict in ('CRISP_REVIEW: PASS', 'CRISP_REVIEW: PASS.',
                'CRISP_REVIEW: PASS - no defects'):
            with self.subTest(verdict=verdict):
                self.assertTrue(review_passed(verdict + '\nChecked the affected callers.', True))

    def test_missing_incomplete_ambiguous_and_rejecting_reports_fail(self):
        for report, ran_commands in [
            ('', True),
            ('No findings.', True),
            ('Review was interrupted.', True),
            ('Unable to assess the callback lifetime.', True),
            ('CRISP_REVIEW: FAIL\nMissing API context.', True),
            ('CRISP_REVIEW: PASS', False),
            ('CRISP_REVIEW: PASSING', True),
            ('CRISP_REVIEW: PASS_FAIL', True),
            ('CRISP_REVIEW: PASS\nCRISP_REVIEW: FAIL', True),
            ('CRISP_REVIEW: PASS.\nCRISP_REVIEW: FAIL', True),
            ('CRISP_REVIEW: PASS - no defects', False),
            ('CRISP_REVIEW: PASS.\n  - [P2] Missed cleanup', True),
            ('CRISP_REVIEW: PASS\n' + REPORT, True),
            ('CRISP_REVIEW: PASS\n  - [P2] Missed cleanup', True),
        ]:
            with self.subTest(report=report, ran_commands=ran_commands):
                self.assertFalse(review_passed(report, ran_commands))
