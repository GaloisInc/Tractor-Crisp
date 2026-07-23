//! Adversarial corpus: pins the checker's current verdict on each known
//! gaming construction.  Tests asserting a PASS document holes that later
//! hardening commits are expected to flip.

use std::fs;
use std::path::PathBuf;
use std::process::Command;
use insta;

struct Outcome {
    passed: bool,
    stdout: String,
}

fn run_scenario(name: &str, with_baseline: bool) -> Outcome {
    let tmp = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(name);
    fs::create_dir_all(&tmp).unwrap();
    let fixture = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests/adversarial")
        .join(name);
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
            .env("FIND_UNSAFE2_SRC_DIR", &fixture)
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

// An unrelated earlier closure rekeys a later unsafe closure: spurious reject.
#[test]
fn closure_reindex() {
    assert_rejected("closure_reindex");
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

// HOLE: removing an export attribute passes; old entry points skip all checks.
#[test]
fn entry_point_demotion() {
    assert_accepted("entry_point_demotion");
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

// HOLE: deleting an exported static passes; statics never carry the export flag.
#[test]
fn exported_static_removed() {
    assert_accepted("exported_static_removed");
}

// HOLE: dropping #[no_mangle] from a static passes for the same reason.
#[test]
fn exported_static_demoted() {
    assert_accepted("exported_static_demoted");
}

// HOLE: static mut -> static keeps the symbol; unsound if C still writes it.
#[test]
fn exported_static_mut_demoted() {
    assert_accepted("exported_static_mut_demoted");
}

// HOLE: only an export boolean is recorded, so a changed export_name passes.
#[test]
fn export_name_value_changed() {
    assert_accepted("export_name_value_changed");
}

// The same exported symbol on a renamed item is spuriously rejected today.
#[test]
fn export_symbol_moved() {
    assert_rejected("export_symbol_moved");
}

// HOLE: pointers materialized from integers via safe std calls are uncharged.
#[test]
fn int_to_ptr_laundering() {
    assert_accepted("int_to_ptr_laundering");
}

// A lifetime-forging helper is rejected only via its deref; its signature
// hides the pointer inside a struct.
#[test]
fn struct_carried_ptr_sig() {
    assert_rejected("struct_carried_ptr_sig");
}

// A helper closure naming a raw pointer is spuriously rejected as a new item.
#[test]
fn closure_ptr_param() {
    assert_rejected("closure_ptr_param");
}

// HOLE: impl code may call an exempt entry point without any charge.
#[test]
fn entry_point_call_from_impl() {
    assert_accepted("entry_point_call_from_impl");
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

// HOLE: `unsafe impl Send` is uncounted.
#[test]
fn unsafe_impl_send() {
    assert_accepted("unsafe_impl_send");
}

// HOLE: inline asm carries no charge, even in a new function.
#[test]
fn asm_uncharged() {
    assert_accepted("asm_uncharged");
}

// HOLE: a crate with no baseline JSON is skipped entirely.
#[test]
fn unknown_crate_skip() {
    let out = run_scenario("unknown_crate_skip", false);
    assert!(out.passed, "unknown_crate_skip: expected (current-behavior) pass");
    assert_eq!(out.stdout, "");
}
