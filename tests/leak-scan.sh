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
# The pattern itself is a leak: it names the very hostnames, domain and wake
# phrase the scan exists to keep out of this public tree (it shipped once, in
# #142, and had to be scrubbed forward). So it lives OUTSIDE the repo -- first
# of: $GS_LEAK_PATTERNS (an ERE), or the file $GS_LEAK_PATTERN_FILE, or
# ~/.config/speech-to-cli/leak-patterns (one ERE, first non-comment line).
# No source => SETUP FAILURE (exit 2), never a silent green.
_pat_file="${GS_LEAK_PATTERN_FILE:-$HOME/.config/speech-to-cli/leak-patterns}"
if [ -n "${GS_LEAK_PATTERNS:-}" ]; then
    PAT="$GS_LEAK_PATTERNS"
elif [ -r "$_pat_file" ]; then
    PAT="$(grep -vE '^\s*(#|$)' "$_pat_file" | head -1)"
else
    echo "!! SETUP FAILURE: no leak pattern source (set GS_LEAK_PATTERNS or create $_pat_file)"
    exit 2
fi
[ -n "$PAT" ] || { echo "!! SETUP FAILURE: empty leak pattern in $_pat_file"; exit 2; }
# The control token is the LAST alternative of the pattern, planted verbatim,
# so the control proves the instrument reads THIS pattern (not a hard-coded one).
_ctrl="${PAT##*|}"
SELF="tests/leak-scan.sh"

# Positive control BEFORE trusting a zero: prove the instrument can see.
if ! printf 'planted %s planted\n' "$_ctrl" | grep -qE "$PAT"; then
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
