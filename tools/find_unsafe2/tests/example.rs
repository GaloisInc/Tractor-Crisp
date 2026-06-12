use std::fs;
use std::process::{Command, Stdio};
use insta;

#[test]
fn run_example() {
    let status = Command::new(env!("CARGO_BIN_EXE_find_unsafe2"))
        .arg("find_unsafe2")
        .arg("example-old/src/lib.rs")
        .args(["--crate-type", "rlib"])
        .args(["--edition", "2024"])
        .args(["--out-dir", env!("CARGO_TARGET_TMPDIR")])
        .env("FIND_UNSAFE2_SRC_DIR", env!("CARGO_MANIFEST_DIR"))
        .env("FIND_UNSAFE2_JSON_DIR", env!("CARGO_TARGET_TMPDIR"))
        .status().unwrap();
    assert!(status.success(), "subcommand failed");

    let output = Command::new(env!("CARGO_BIN_EXE_check_unsafe2"))
        .arg("check_unsafe2")
        .arg("example-new/src/lib.rs")
        .args(["--crate-type", "rlib"])
        .args(["--edition", "2024"])
        .args(["--out-dir", env!("CARGO_TARGET_TMPDIR")])
        .env("FIND_UNSAFE2_JSON_DIR", env!("CARGO_TARGET_TMPDIR"))
        .stderr(Stdio::inherit())
        .output().unwrap();
    assert!(!output.status.success(), "subcommand succeeded unexpectedly");
    let stdout = String::from_utf8(output.stdout).unwrap();
    insta::assert_snapshot!(stdout);
}

#[test]
fn missing_baseline_json_is_reported() {
    let json_dir = std::path::Path::new(env!("CARGO_TARGET_TMPDIR")).join("missing-baseline-json");
    let _ = fs::remove_dir_all(&json_dir);
    fs::create_dir_all(&json_dir).unwrap();

    let output = Command::new(env!("CARGO_BIN_EXE_check_unsafe2"))
        .arg("check_unsafe2")
        .arg("example-new/src/lib.rs")
        .args(["--crate-type", "rlib"])
        .args(["--edition", "2024"])
        .args(["--out-dir", env!("CARGO_TARGET_TMPDIR")])
        .env("FIND_UNSAFE2_SRC_DIR", env!("CARGO_MANIFEST_DIR"))
        .env("FIND_UNSAFE2_JSON_DIR", &json_dir)
        .output().unwrap();

    assert!(!output.status.success(), "subcommand succeeded unexpectedly");
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(
        stderr.contains("missing unsafe baseline JSON"),
        "missing diagnostic in stderr:\n{stderr}",
    );
}
