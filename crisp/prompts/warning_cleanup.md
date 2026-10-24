Fix compiler warnings in the Rust project at `{cargo_dir_path}`. `cargo fix` has already run. The remaining diagnostics are in `COMPILER_WARNINGS.txt`; their paths are relative to the Rust project. Original C sources and tests are available at their original paths for reference.

Fix causes shared by many warnings first. Confirm type equivalence against the C sources before consolidating definitions. Preserve behavior and the exported ABI. Do not silence lints, broaden existing allowances, change build configuration, or delete needed code to lower the warning count. Leave warnings alone when their fixes cannot be justified.

Run `cargo build --manifest-path {cargo_dir_path}/Cargo.toml` to check your changes. The original tests will be run against the cleaned-up code, and it will be subject to an independent safety review. Return a coherent partial cleanup if necessary; you only get one attempt.

{ffi_entry_point_rules}
