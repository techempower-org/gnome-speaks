#!/usr/bin/env bash
#
# Leak scan over the TRACKED tree: this repo is public, and LAN hostnames, LAN
# IPs, the HA domain and the maintainer's home path must never enter git (see
# CLAUDE.md, "Public repo"). This is the same pattern every agent is told to
# run on its diff -- kept here so main itself cannot regress silently (#126:
# the tracked systemd unit hard-coded the maintainer's home path for weeks).
#
# Exit contract, shared with tests/repros/run_all.sh:
#   0 clean · 1 a hit (the defect is present) · 2 SETUP FAILURE
#
# Scans `git grep` (tracked files only) from the repo root, so a gitignored
# tmp/ or an untracked scratch file never fails the gate. This script is
# excluded from its own scan -- it necessarily contains the pattern, and a
# scanner that flags itself is the self-match trap from CLAUDE.md.
set -u
set -o pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PAT='familiar|donk|10\.0\.6\.|jphe\.in|/home/jp'
SELF="tests/leak-scan.sh"

# Positive control BEFORE trusting a zero: prove the instrument can see.
if ! printf '%s\n' 'planted /home/jp planted' | grep -qE "$PAT"; then
    echo "!! SETUP FAILURE: leak pattern does not match its own control"
    exit 2
fi
git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
    echo "!! SETUP FAILURE: $REPO is not a git work tree"
    exit 2
}

hits=$(git -C "$REPO" grep -nE "$PAT" -- . ":(exclude)$SELF")
st=$?
case "$st" in
    0) printf '%s\n' "$hits"
       echo "FAIL: $(printf '%s\n' "$hits" | wc -l) leak-pattern hit(s) in tracked files"
       exit 1 ;;
    1) echo "PASS: no leak-pattern hits in tracked files"
       exit 0 ;;
    *) echo "!! SETUP FAILURE: git grep exited $st"
       exit 2 ;;
esac
