Review the uncommitted changes to the Rust project in `{cargo_dir_path}` ONLY for violations of the FFI entry point rules below.  The changes were made by a refactoring agent whose goal is to make the implementation code fully safe; ordinary code-review concerns (bugs, style, performance) are out of scope unless they violate these rules.

Focus on the FFI entry points touched by the diff.  Read the surrounding code as needed for context; in particular, follow the definition of any macro invoked from an entry point to check for logic hidden in macro expansions.

{ffi_entry_point_rules}

Report only genuine rule violations present in the changed code; if there are none, report no findings.  For each violation, cite the entry point, the rule violated, and the offending code.
