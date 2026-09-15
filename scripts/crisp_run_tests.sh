#!/bin/bash
set -euo pipefail

# Ask the CRISP harness to run the test suite.  CRISP will extract a snapshot
# of the translated Rust code from the current sandbox, run the tests on it,
# and output the results.  Note that this always uses the original version of
# the tests; changes to the test code are not allowed and will be ignored.
# This script exits 0 if all tests pass or nonzero if any of them fail.

curl -X POST --fail-with-body \
    -H "Authorization: Bearer $CRISP_INTERNAL_API_KEY" \
    http://$CRISP_INTERNAL_API_HOST:$CRISP_INTERNAL_API_PORT/crisp/run_tests
