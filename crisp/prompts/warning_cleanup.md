Fix compiler warnings in the Rust project at `{cargo_dir_path}`. `cargo fix` has already run. The remaining diagnostics are in `COMPILER_WARNINGS.txt`; their paths are relative to the Rust project. Original C sources and tests are available at their original paths for reference.

Fix causes shared by many warnings first, especially conflicting foreign declarations and duplicated C types. Confirm type equivalence against the C sources before consolidating definitions. Preserve behavior and the exported ABI. Do not silence lints, broaden existing allowances, change build configuration, or delete needed code to lower the warning count. Leave warnings alone when their fixes cannot be justified.

Run `cargo build --manifest-path {cargo_dir_path}/Cargo.toml` to check your changes. CRISP will run the unsafe checker, original tests and independent safety review afterward. Return a coherent partial cleanup if necessary; there is only one cleanup attempt before safety planning.

{checker_rules}

{ffi_entry_point_rules}
