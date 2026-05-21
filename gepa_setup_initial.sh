#!/usr/bin/env bash

###############################################################################
# Run this script to prepare any dataset directory for GEPA
# The dataset directory must be inside `Test-Corpus/Public-Tests/`
# Pass only the dataset name as an argument, not the whole path
# E.g. `./gepa_setup_initial B01_organic`

# This script runs the CRISP workflow on all projects inside the dataset
# It will use C2Rust to convert C code to unsafe Rust
# It will *not* do any LLM rewriting
# At the end of running this script, the 'current' node of all projects
# inside the passed dataset will point to unsafe Rust code

# This script will also place an entire copy of
# `Test-Corpus/Public-Tests/<dataset_dir>` in the parent location of this repo
# In case any of the nodes inside projects change,
# this copy can be used as a backup instead of running this script again
###############################################################################

dir="$1"

count=0
for folder in "Test-Corpus/Public-Tests/$dir"/*/; do
    LLM_SAFETY_TRIES=0 python scripts/test_eval.py "$folder"
    ((count++))
    echo "==================== DONE: $count ===================="
done

cp -r "./Test-Corpus/Public-Tests/$dir" "../${dir}_gepaready_backup"
