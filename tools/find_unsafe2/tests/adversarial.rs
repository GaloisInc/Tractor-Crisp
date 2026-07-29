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
        "{name}: expected (current-behavior) pass, got:\n{}",
        out.stdout
    );
    assert_eq!(out.stdout, "", "{name}: expected no diagnostics");
}

// A new laundering helper is rejected even though the global count drops.
#[test]
fn as_ref_helper() {
    assert_rejected("as_ref_helper");
}

// Growing an already-unsafe fn is rejected even with a global net decrease.
#[test]
fn hub_conversion() {
    assert_rejected("hub_conversion");
}

// Trading many derefs for one unsafe call in the same fn is rejected.
#[test]
fn count_compression() {
    assert_rejected("count_compression");
}

// A binding closure created inside an FFI entry point inherits its exemption.
#[test]
fn entry_point_closure_exempt() {
    assert_accepted("entry_point_closure_exempt");
}

// Renaming an unsafe fn reads as a new function: spurious reject.
#[test]
fn rename_unsafe_fn() {
    assert_rejected("rename_unsafe_fn");
}

// Safe union construction is charged (documented overapproximation).
#[test]
fn union_construction_charged() {
    assert_rejected("union_construction_charged");
}

// Removing an export attribute from an entry point is an ABI break.
#[test]
fn entry_point_demotion() {
    assert_rejected("entry_point_demotion");
}

// Deleting an entry point outright is an ABI break.
#[test]
fn entry_point_removed() {
    assert_rejected("entry_point_removed");
}

// Promoting an impl fn to an entry point would move its unsafety out of the total.
#[test]
fn entry_point_promotion() {
    assert_rejected("entry_point_promotion");
}

// A new raw-pointer field is charged against the type.
#[test]
fn new_ptr_field() {
    assert_rejected("new_ptr_field");
}

// Deleting an exported static is an ABI break.
#[test]
fn exported_static_removed() {
    assert_rejected("exported_static_removed");
}

// Dropping #[no_mangle] from a static is an ABI break.
#[test]
fn exported_static_demoted() {
    assert_rejected("exported_static_demoted");
}

// HOLE: static mut -> static keeps the symbol; unsound if C still writes it.
#[test]
fn exported_static_mut_demoted() {
    assert_accepted("exported_static_mut_demoted");
}

// Changing an export_name value is an ABI break.
#[test]
fn export_name_value_changed() {
    assert_rejected("export_name_value_changed");
}

// The same exported symbol on a renamed item keeps the ABI intact.
#[test]
fn export_symbol_moved() {
    assert_accepted("export_symbol_moved");
}

// Pointers materialized from integers via safe std calls are charged as casts.
#[test]
fn int_to_ptr_laundering() {
    assert_rejected("int_to_ptr_laundering");
}

// A lifetime-forging helper is rejected only via its deref; its signature
// hides the pointer inside a struct.
#[test]
fn struct_carried_ptr_sig() {
    assert_rejected("struct_carried_ptr_sig");
}

// A helper closure's raw-pointer param doesn't charge the enclosing signature.
#[test]
fn closure_ptr_param() {
    assert_accepted("closure_ptr_param");
}

// Impl code routing a call through an exempt entry point is charged.
#[test]
fn entry_point_call_from_impl() {
    assert_rejected("entry_point_call_from_impl");
}

// HOLE: making a foreign decl safe strips its contract and lowers the count.
#[test]
fn safe_foreign_decl() {
    assert_accepted("safe_foreign_decl");
}

// HOLE: offset_from laundered through ptr-as-isize subtraction is uncounted.
#[test]
fn ptr_as_isize_arith() {
    assert_accepted("ptr_as_isize_arith");
}

// A new `unsafe impl Send`/`Sync` asserts what no metric can verify: an error.
#[test]
fn unsafe_impl_send() {
    assert_rejected("unsafe_impl_send");
}

// Inline asm is charged, even in a new function.
#[test]
fn asm_charged() {
    assert_rejected("asm_charged");
}

// A project crate with no baseline inventory must not add new unsafety.
#[test]
fn unknown_crate_skip() {
    let out = run_scenario("unknown_crate_skip", false);
    assert!(!out.passed, "unknown_crate_skip: expected rejection, got pass");
    insta::assert_snapshot!("unknown_crate_skip", out.stdout);
}

// A project crate with no baseline inventory is accepted if it adds no unsafety.
#[test]
fn new_crate_safe() {
    let out = run_scenario("new_crate_safe", false);
    assert!(out.passed, "new_crate_safe: expected pass, got:\n{}", out.stdout);
    assert_eq!(out.stdout, "", "new_crate_safe: expected no diagnostics");
}

// A new crate holding only type definitions is still a project crate.
#[test]
fn type_only_crate() {
    let out = run_scenario("type_only_crate", false);
    assert!(!out.passed, "type_only_crate: expected rejection, got pass");
    insta::assert_snapshot!("type_only_crate", out.stdout);
}

// A crate compiled from outside `SRC_DIR` is a dependency, not a project crate, so its unsafety
// is not held against an empty baseline.
#[test]
fn dependency_crate_skipped() {
    let src_dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join("dependency_crate_skipped_src");
    fs::create_dir_all(&src_dir).unwrap();
    let out = run_scenario_in("dependency_crate_skipped", false, Some(&src_dir));
    assert!(out.passed, "dependency_crate_skipped: expected pass, got:\n{}", out.stdout);
    assert_eq!(out.stdout, "", "dependency_crate_skipped: expected no diagnostics");
}
