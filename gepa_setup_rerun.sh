#!/usr/bin/env bash

# Run this script to refresh any dataset directory before re-running GEPA
# Pass only the dataset name as an argument, not the whole path
# E.g. `./gepa_setup_rerun B01_organic`

dir="$1"

cd Test-Corpus/Public-Tests
git clean -df
rm -rf "$dir"
cp -r "../../../${dir}_gepaready_backup" "./$dir"
cd ../..
