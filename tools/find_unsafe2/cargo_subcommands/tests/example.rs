use std::process::{Command, Stdio};

#[test]
fn run_check_after_cargo_build() {
    let fixture_dir = std::fs::canonicalize(concat!(env!("CARGO_MANIFEST_DIR"), "/..")).unwrap();

    // HACK: Manually build the find_unsafe2 and check_unsafe2 binaries. We need
    // these binaries to test the Cargo subcommands, but Cargo doesn't expose a way
    // for us to list those binaries as a dependency of this crate, so they won't
    // automatically be built if they don't exist.
    let status = Command::new("cargo")
        .current_dir(&fixture_dir)
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

    let json_dir = format!(
        "{}/run_check_after_cargo_build",
        env!("CARGO_TARGET_TMPDIR")
    );

    // Do the initial run of find-unsafe2 to generate the unsafe count JSON.
    let status = Command::new(env!("CARGO_BIN_EXE_cargo-find-unsafe2"))
        .current_dir(&fixture_dir)
        .arg("find-unsafe2")
        .args(["--manifest-path", "example-old/Cargo.toml"])
        .env("FIND_UNSAFE2_SRC_DIR", &fixture_dir)
        .env("FIND_UNSAFE2_JSON_DIR", &json_dir)
        .status()
        .unwrap();
    assert!(status.success(), "cargo-find-unsafe2 failed");

    // Run `cargo build` on the workspace before running the unsafe checker.
    let status = Command::new("cargo")
        .current_dir(&fixture_dir)
        .args(["build", "--manifest-path", "example-new/Cargo.toml"])
        .status()
        .unwrap();
    assert!(status.success(), "cargo build failed");

    // Run `cargo check-unsafe2` after building the workspace to confirm that it
    // still reports increased unsafe ops.
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
