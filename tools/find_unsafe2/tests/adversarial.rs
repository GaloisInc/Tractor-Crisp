//! Adversarial corpus: pins the checker's current verdict on each known
//! gaming construction.  Tests asserting a PASS document holes that later
//! hardening commits are expected to flip.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use insta;

struct Outcome {
    passed: bool,
    stdout: String,
}

fn run_scenario(name: &str, with_baseline: bool) -> Outcome {
    run_scenario_in(name, with_baseline, None)
}

/// Like `run_scenario`, but with `SRC_DIR` pointed somewhere other than the fixture, so the
/// scenario compiles as a crate outside the project.
fn run_scenario_in(name: &str, with_baseline: bool, src_dir: Option<&Path>) -> Outcome {
    let tmp = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(name);
    fs::create_dir_all(&tmp).unwrap();
    let fixture = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests/adversarial")
        .join(name);
    let src_dir = src_dir.unwrap_or(&fixture);
    let crate_name = format!("adv_{name}");

    if with_baseline {
        let status = Command::new(env!("CARGO_BIN_EXE_find_unsafe2"))
            .arg("find_unsafe2")
            .arg(fixture.join("old.rs"))
            .args(["--crate-type", "rlib"])
            .args(["--edition", "2024"])
            .args(["--crate-name", &crate_name])
            .arg("--out-dir")
            .arg(&tmp)
            .env("FIND_UNSAFE2_SRC_DIR", src_dir)
            .env("FIND_UNSAFE2_JSON_DIR", &tmp)
            .status()
            .unwrap();
        assert!(status.success(), "find_unsafe2 failed for {name}");
    }

    let output = Command::new(env!("CARGO_BIN_EXE_check_unsafe2"))
        .arg("check_unsafe2")
        .arg(fixture.join("new.rs"))
        .args(["--crate-type", "rlib"])
        .args(["--edition", "2024"])
        .args(["--crate-name", &crate_name])
        .arg("--out-dir")
        .arg(&tmp)
        .env("FIND_UNSAFE2_SRC_DIR", src_dir)
        .env("FIND_UNSAFE2_JSON_DIR", &tmp)
        .output()
        .unwrap();
    Outcome {
        passed: output.status.success(),
        stdout: String::from_utf8(output.stdout).unwrap(),
    }
}

fn assert_rejected(name: &str) {
    let out = run_scenario(name, true);
    assert!(!out.passed, "{name}: expected rejection, got pass");
    insta::assert_snapshot!(name.to_owned(), out.stdout);
}

fn assert_accepted(name: &str) {
    let out = run_scenario(name, true);
    assert!(
        out.passed,
        "{name}: expected pass, got:\n{}",
        out.stdout
    );
    assert_eq!(out.stdout, "", "{name}: expected no diagnostics");
}

macro_rules! tests_assert_rejected {
    ($($name:ident,)*) => {
        $(
            #[test]
            fn $name() {
                assert_rejected(stringify!($name));
            }
        )*
    };
}

macro_rules! tests_assert_accepted {
    ($($name:ident,)*) => {
        $(
            #[test]
            fn $name() {
                assert_accepted(stringify!($name));
            }
        )*
    };
}

tests_assert_rejected! {
    as_ref_helper,
    // Similar to as_ref_helper.
    struct_carried_ptr_sig,
    hub_conversion,
    count_compression,
    // Renaming a function containing unsafe looks the same as deleting the original (okay) and
    // adding a new one with the same body (rejected).
    rename_unsafe_fn,
    // Renaming an exported function (without changing its symbol) is rejected, just like renaming
    // a non-exported one.
    export_symbol_moved,
    new_ptr_field,
    // Moves unsafe code into a closure, which counts as a separate function.
    closure_ptr_param,
    // Changing the set of exported symbols (adding or removing) is not allowed.
    entry_point_demotion,
    entry_point_promotion,
    export_name_value_changed,
    // Calls an exported FFI function from implementation code.
    entry_point_call_from_impl,
    inline_asm,

    // Calling `safe fn` FFI imports still counts toward the `uses_foreign_fn` progress metric, but
    // doesn't count toward `calls_unsafe`.  Probably we should instead count every `safe fn` as an
    // unsafe declaration, so this would fail for a different reason.
    safe_foreign_decl,

    // Spuriously rejected:

    closure_reindex,
    // Union construction is counted as a field use, and union field uses are unsafe (known
    // limitation).
    union_construction_charged,
}

tests_assert_accepted! {
    exported_static_removed,
    exported_static_demoted,

    // Incorrectly accepted:

    // Dropping `mut` from an exported static may introduce unsoundness if C code writes to it.
    exported_static_mut_demoted,

    // Tests various methods of converting `usize` to a pointer beyond `x as *mut T`.
    int_to_ptr_laundering,

    // Adding `unsafe impl`s should count as adding unsafety.
    unsafe_impl_send,
}

/// A crate with no baseline JSON should be handled as if it had a baseline with zero unsafe.
#[test]
fn new_crate() {
    let out = run_scenario("new_crate", false);
    assert!(!out.passed, "new_crate: expected rejection, got pass");
    insta::assert_snapshot!("new_crate", out.stdout);
}

/// Like `new_crate`, but the crate has only type definitions.  It still gets treated as a project
/// crate because the type definition spans are inside the `SRC_DIR`, and it's rejected because a
/// field contains a raw pointer.
#[test]
fn new_crate_type_only() {
    let out = run_scenario("new_crate_type_only", false);
    assert!(!out.passed, "new_crate_type_only: expected rejection, got pass");
    insta::assert_snapshot!("new_crate_type_only", out.stdout);
}

/// A crate outside the project is considered a dependency, and we don't check it for unsafe code.
#[test]
fn new_dependency_crate() {
    let src_dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join("new_dependency_crate");
    fs::create_dir_all(&src_dir).unwrap();
    let out = run_scenario_in("new_dependency_crate", false, Some(&src_dir));
    assert!(out.passed, "new_dependency_crate: expected pass, got:\n{}", out.stdout);
    assert_eq!(out.stdout, "", "new_dependency_crate: expected no diagnostics");
}
