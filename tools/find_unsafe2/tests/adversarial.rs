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
    // Forbid creating a new FFI entry point.
    entry_point_promotion,
    new_ptr_field,
    // Moves unsafe code into a closure, which counts as a separate function.
    closure_ptr_param,

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
    // Symbol name was changed, without adding or removing the export flag.
    export_name_value_changed,

    // Incorrectly accepted:

    // The old state marked the function as an FFI entry point, so all checks are disabled on that
    // function in the new state.
    entry_point_demotion,

    // Dropping `mut` from an exported static may introduce unsoundness if C code writes to it.
    exported_static_mut_demoted,

    // Tests various methods of converting `usize` to a pointer beyond `x as *mut T`.
    int_to_ptr_laundering,

    // Calls an exported FFI function from implementation code.
    entry_point_call_from_impl,

    // Adding `unsafe impl`s should count as adding unsafety.
    unsafe_impl_send,

    // Inline assembly currently isn't counted as an unsafe operation.
    inline_asm,
}

// Incorrectly accepted: a crate with no baseline JSON is skipped entirely.
#[test]
fn unknown_crate_skip() {
    let out = run_scenario("unknown_crate_skip", false);
    assert!(out.passed, "unknown_crate_skip: expected (current-behavior) pass");
    assert_eq!(out.stdout, "");
}
