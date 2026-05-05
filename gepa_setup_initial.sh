#!/usr/bin/env bash

# Run this script to prepare any dataset directory for GEPA
# Pass only the dataset name as an argument, not the whole path
# E.g. `./gepa_setup_initial B01_organic`

dir="$1"

count=0
for folder in "Test-Corpus/Public-Tests/$dir"/*/; do
    LLM_SAFETY_TRIES=0 python scripts/test_eval.py "$folder"
    ((count++))
    echo "==================== DONE: $count ===================="
done

cp -r "./Test-Corpus/Public-Tests/$dir" "../${dir}_gepaready_backup"
