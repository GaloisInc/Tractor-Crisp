#!/bin/bash
set -euo pipefail

# Ask the CRISP harness to run the FFI review pass.  CRISP will extract a
# snapshot of the translated Rust code from the current sandbox, run the
# reviewer on it, and output the results.  This script exits 0 if the reviewer
# approves the FFI changes or if there are no changes requiring approval, and
# exits nonzero if the changes were rejected.

curl -X POST --fail-with-body \
    -H "Authorization: Bearer $CRISP_INTERNAL_API_KEY" \
    http://$CRISP_INTERNAL_API_HOST:$CRISP_INTERNAL_API_PORT/crisp/ffi_review
