#!/bin/bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
    echo 'usage: crisp_submit_step_plan.sh STEPS.json' 1>&2
    exit 2
fi

# Submit the proposed plan as JSON; validation errors are printed by curl.
curl -X POST --fail-with-body \
    -H "Authorization: Bearer $CRISP_INTERNAL_API_KEY" \
    -H 'Content-Type: application/json' \
    --data-binary "@$1" \
    "http://$CRISP_INTERNAL_API_HOST:$CRISP_INTERNAL_API_PORT/crisp/submit_step_plan"
