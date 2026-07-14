use std::env;

fn main() {
    // RUSTC_WRAPPER handling
    let self_exe = env::current_exe().unwrap();
    let wrapper_exe = self_exe.parent().unwrap().join("expand_modules");

    find_unsafe2_cargo_subcommands::cargo_subcommand_main(&wrapper_exe);
}
