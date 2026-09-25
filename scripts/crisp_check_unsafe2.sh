#!/bin/bash
set -euo pipefail

# Ask the CRISP harness to check for new unsafe code.  CRISP will extract a
# snapshot of the translated Rust code from the current sandbox and then run
# `cargo check-unsafe2` on it, using records of the unsafe code from the
# initial version of the Rust code as a baseline.  This script exits nonzero if
# there's any new unsafe (or unsafe-adjacent) code relative to the baseline.

curl -X POST --fail-with-body \
    -H "Authorization: Bearer $CRISP_INTERNAL_API_KEY" \
    http://$CRISP_INTERNAL_API_HOST:$CRISP_INTERNAL_API_PORT/crisp/check_unsafe2
