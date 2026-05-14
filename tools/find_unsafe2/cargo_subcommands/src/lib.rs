use std::env;
use std::path::{self, Path};
use std::process::{self, Command};
use std::os::unix::process::CommandExt;

fn rustc_print_sysroot(opt_toolchain: Option<&str>) -> String {
    let mut cmd = Command::new("rustc");
    if let Some(toolchain) = opt_toolchain {
        cmd.arg(format!("+{toolchain}"));
    }
    let output = cmd
        .args(["--print", "sysroot"])
        .stderr(process::Stdio::inherit())
        .output().unwrap();
    assert!(output.status.success(), "{:?} exited with code {:?}", cmd, output.status);
    String::from_utf8(output.stdout).unwrap()
}

pub fn cargo_subcommand_main(wrapper_exe: &Path) -> ! {
    let opt_toolchain = option_env!("RUSTUP_TOOLCHAIN");

    // LD_LIBRARY_PATH handling
    let sysroot = rustc_print_sysroot(opt_toolchain);

    #[cfg(target_os = "linux")]
    const LIB_PATH_VAR: &str = "LD_LIBRARY_PATH";
    #[cfg(target_os = "macos")]
    const LIB_PATH_VAR: &str = "DYLD_LIBRARY_PATH";

    let add_lib_path = format!("{sysroot}/lib");
    let new_lib_path = match env::var(LIB_PATH_VAR) {
        Ok(lib_path) => format!("{add_lib_path}:{lib_path}"),
        Err(env::VarError::NotPresent) => add_lib_path,
        Err(env::VarError::NotUnicode(_)) => panic!("bad value for ${}", LIB_PATH_VAR),
    };

    // FIND_UNSAFE2_SRC_DIR handling
    const SRC_DIR_VAR: &str = "FIND_UNSAFE2_SRC_DIR";
    let opt_src_dir = env::var_os(SRC_DIR_VAR);
    let src_dir = opt_src_dir.as_ref().map_or(Path::new("."), |x| Path::new(x));
    let src_dir_abs = path::absolute(&src_dir).unwrap();

    // FIND_UNSAFE2_JSON_DIR handling
    const JSON_DIR_VAR: &str = "FIND_UNSAFE2_JSON_DIR";
    let opt_json_dir = env::var_os(JSON_DIR_VAR);
    let json_dir = opt_json_dir.as_ref().map_or(Path::new("find_unsafe2_json"), |x| Path::new(x));
    let json_dir_abs = path::absolute(&json_dir).unwrap();

    let opt_cargo_bin = env::var_os("CARGO");
    let cargo_bin = opt_cargo_bin.as_ref().map_or(Path::new("cargo"), |x| Path::new(x));

    let mut cmd = Command::new(cargo_bin);
    if let Some(toolchain) = opt_toolchain {
        cmd.arg(format!("+{toolchain}"));
    }
    cmd.arg("build")
        .args(env::args().skip(2))
        .env(LIB_PATH_VAR, new_lib_path)
        .env("RUSTC_WRAPPER", wrapper_exe)
        .env(SRC_DIR_VAR, src_dir_abs)
        .env(JSON_DIR_VAR, json_dir_abs);
    eprintln!("exec: {cmd:?}");
    let err = cmd.exec();
    panic!("exec failed: {:?}", err);
}
