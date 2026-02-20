#!/bin/bash
set -euo pipefail

# Extract the `tools/` subdirectory from the git history of the current branch.
# This produces a new history containing only `tools/`.  Commits that don't
# modify `tools/` are discarded, and commits that modify `tools/` along with
# other files are change to modify only `tools/`.  This is similar to the
# behavior of `git subtree split`, but this script doesn't move the contents of
# the subdirectory up to the root of the repository.  The resulting commits can
# be rebased or cherry-picked from the TRACTOR-CRISP repo into c2rust or vice
# versa.

# Create a temporary tag for `git filter-branch` to modify.
tmp_tag="extract-tools-for-c2rust-$$"
git tag "$tmp_tag"

# We want to use `--index-filter` to remove all files except the subdirectories
# of interest.  This works great for commits after the subdirectories were
# created, but for earlier commits, the input to `git-update-index` is empty,
# and that tool seems quite reluctant to create an empty index file when
# running in `--index-info` mode.  We work around this by creating an empty
# file `empty.txt` (e69de29... is the hash of the empty blob) and then deleting
# it from the index afterward.

FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch --index-filter '
    {
        git ls-files -s | grep -e "$(printf "\\ttools/")" ; \
        echo  "100644 e69de29bb2d1d6434b8b29ae775ad8c2e48c5391 0	empty.txt" ; \
    } | GIT_INDEX_FILE=$GIT_INDEX_FILE.new git update-index --index-info \
    && GIT_INDEX_FILE=$GIT_INDEX_FILE.new git update-index --force-remove empty.txt \
    && mv "$GIT_INDEX_FILE.new" "$GIT_INDEX_FILE"
    ' --prune-empty "$tmp_tag"

# Report the new commit, then delete the temporary tag.
echo "extracted $(git rev-parse "$tmp_tag")"
git tag -d "$tmp_tag"
