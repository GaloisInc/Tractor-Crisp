import contextlib
import io
import tempfile
import unittest

from crisp.mvir import MVIR, FileNode, TreeNode
from crisp.workflow import (
    FFI_SEEN_FINDINGS_CAP, Workflow, merge_ffi_finding_titles,
    upgrade_toolchain_rust_src,
)


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
