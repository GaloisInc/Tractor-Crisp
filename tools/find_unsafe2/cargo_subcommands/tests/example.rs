use std::path::Path;
use std::process::{Command, Stdio};

// HACK: Manually build the find_unsafe2 and check_unsafe2 binaries. We need
// these binaries to test the Cargo subcommands, but Cargo doesn't expose a way
// for us to list those binaries as a dependency of this crate, so they won't
// automatically be built if they don't exist.
fn build_wrappers(fixture_dir: &Path) {
    let status = Command::new("cargo")
        .current_dir(fixture_dir)
        .args([
            "build",
            "-p",
            "find_unsafe2",
            "--bin",
            "find_unsafe2",
            "--bin",
            "check_unsafe2",
        ])
        .status()
        .unwrap();
    assert!(status.success(), "failed to build wrapper binaries");
}

#[test]
fn run_check_after_cargo_build() {
    let fixture_dir = std::fs::canonicalize(concat!(env!("CARGO_MANIFEST_DIR"), "/..")).unwrap();

    build_wrappers(&fixture_dir);

    let json_dir = format!(
        "{}/run_check_after_cargo_build",
        env!("CARGO_TARGET_TMPDIR")
    );

    let status = Command::new(env!("CARGO_BIN_EXE_cargo-find-unsafe2"))
        .current_dir(&fixture_dir)
        .arg("find-unsafe2")
        .args(["--manifest-path", "example-old/Cargo.toml"])
        .env("FIND_UNSAFE2_SRC_DIR", &fixture_dir)
        .env("FIND_UNSAFE2_JSON_DIR", &json_dir)
        .status()
        .unwrap();
    assert!(status.success(), "cargo-find-unsafe2 failed");

    let status = Command::new("cargo")
        .current_dir(&fixture_dir)
        .args(["build", "--manifest-path", "example-new/Cargo.toml"])
        .status()
        .unwrap();
    assert!(status.success(), "cargo build failed");

    let output = Command::new(env!("CARGO_BIN_EXE_cargo-check-unsafe2"))
        .current_dir(&fixture_dir)
        .arg("check-unsafe2")
        .args(["--manifest-path", "example-new/Cargo.toml"])
        .env("FIND_UNSAFE2_JSON_DIR", &json_dir)
        .stderr(Stdio::inherit())
        .output()
        .unwrap();
    assert!(
        !output.status.success(),
        "cargo-check-unsafe2 succeeded unexpectedly"
    );
}
