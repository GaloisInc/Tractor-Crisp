#!/bin/bash
set -euo pipefail
dir=$(dirname "$0")
cargo install --locked --path "$dir"/find_unsafe
cargo install --locked --path "$dir"/merge_rust
cargo install --locked --path "$dir"/related_decls
cargo install --locked --path "$dir"/split_ffi_entry_points
cargo install --locked --path "$dir"/split_rust
