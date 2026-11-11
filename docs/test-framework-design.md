# Test framework design

See the [implemented framework and usage guide](test-framework.md).

The framework has two standalone operations: compile C test drivers against the
original headers and initial Rust libraries, and run those frozen drivers and
scripts against a Rust version. The initial C2Rust output supplies the reference
observations. Tests use the public C ABI or CLI, print observations, and can
normalize output using ordinary C or script logic.

CRISP wraps these operations in MVIR analysis passes. Compilation tracks the C,
test, and initial Rust trees plus the command. Execution tracks Rust, tests,
compiled binaries, the command, and a trial number. MVIR owns caching and input
identity; the standalone script has no additional fingerprinting or dependency
cache. Comparing observations and selecting accepted references remain separate
higher-level operations.

The original, larger C-reference design is preserved in Git history.
