#!/usr/bin/env bash
#
# Generate the baseline/verdict table in tests/repros/README.md from every
# suite's own tests/repros/<suite>/BASELINE.md, and validate the manifests.
#
#   tests/repros/gen_readme.sh           # rewrite the generated block in place
#   tests/repros/gen_readme.sh --check   # exit 1 if the block is stale; write nothing
#
# WHY (#169): the table used to be hand-edited, and every PR that added a suite
# appended a row at the same spot -- three consecutive PRs conflicted there in
# one night. Now each suite owns its rows in its own directory and this script
# is the ONLY writer of the shared block. Two lanes adding suites touch disjoint
# files; a conflict in the generated block is resolved by re-running this
# script, never by hand.
#
# The block is delimited by two HTML comments (BEGIN/END markers). Everything
# outside them is prose and is left alone. Rows are collected from every
# BASELINE.md in LC_ALL=C order of the suite directory name: every line that
# starts with `| ` except the header row (`| suite |`); the `|---|` separator
# does not match `^| ` so it needs no rule.
#
# --check also validates the manifests, because a suite directory nobody
# collects is the "instrument that cannot see" trap: a lane adds a suite, the
# runner never runs it, the gate stays green.
#   * every suite directory has a BASELINE.md;
#   * every suite directory has a suite.list, except the two wired by hand
#     (prefs-rig has its own invocation shape in run_all.sh; shell-rig is a
#     headless gnome-shell and is deliberately not collected).
#
# Exit contract, shared with tests/repros/run_all.sh:
#   0 clean · 1 stale block / manifest gap (the defect is present) · 2 SETUP FAILURE
set -u
set -o pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
README="$HERE/README.md"
BEGIN='<!-- BEGIN GENERATED: baselines -- written by tests/repros/gen_readme.sh from tests/repros/*/BASELINE.md; edit those, then re-run it. Never edit this block by hand. -->'
END='<!-- END GENERATED: baselines -->'
# Suites collected by hand in run_all.sh (or deliberately not at all). A
# directory listed here needs no suite.list; every other one does.
HAND_WIRED="prefs-rig shell-rig"

[ -f "$README" ] || { echo "!! SETUP FAILURE: no $README"; exit 2; }
check=0
[ "${1:-}" = "--check" ] && check=1

rc=0
gap() { printf '!! %s\n' "$1"; rc=1; }

# --- manifests -------------------------------------------------------------
suites=$(cd "$HERE" && for d in */; do d="${d%/}"; [ "$d" = "__pycache__" ] && continue; echo "$d"; done | LC_ALL=C sort)
[ -n "$suites" ] || { echo "!! SETUP FAILURE: no suite directories under $HERE"; exit 2; }
for s in $suites; do
    [ -f "$HERE/$s/BASELINE.md" ] || gap "$s/: no BASELINE.md (every suite pins its baseline SHA and verdict there)"
    case " $HAND_WIRED " in *" $s "*) continue ;; esac
    [ -f "$HERE/$s/suite.list" ] || gap "$s/: no suite.list -- run_all.sh will NOT collect it (one check per line; see README, Adding a suite)"
done

# --- generated block -------------------------------------------------------
# Rows are validated in THIS shell (gap() must reach rc), then emitted. A gap()
# inside `$(gen)` would run in a subshell: its rc lost, its `!!` line spliced
# into the README as a table row -- measured while writing the control for it.
rows_of() { grep -E '^\| ' "$1" | grep -vE '^\| suite \|'; }
for s in $suites; do
    f="$HERE/$s/BASELINE.md"
    [ -f "$f" ] || continue
    [ -n "$(rows_of "$f")" ] || gap "$s/BASELINE.md has no table rows (a row is a line starting with '| ')"
done
gen() {
    printf '%s\n' "$BEGIN"
    printf '| suite | issue / PR | baseline | expected there |\n|---|---|---|---|\n'
    for s in $suites; do
        f="$HERE/$s/BASELINE.md"
        [ -f "$f" ] || continue
        rows=$(rows_of "$f")
        [ -n "$rows" ] && printf '%s\n' "$rows"
    done
    printf '%s\n' "$END"
}
block=$(gen)

n_begin=$(grep -cF -- "$BEGIN" "$README" || true)
n_end=$(grep -cF -- "$END" "$README" || true)
if [ "$n_begin" != 1 ] || [ "$n_end" != 1 ]; then
    echo "!! SETUP FAILURE: expected exactly one BEGIN and one END marker in $README (found $n_begin / $n_end)"
    exit 2
fi

# Splice: prose before BEGIN, the block, prose after END. awk prints only
# outside the markers; the block is inserted where BEGIN was.
spliced=$(awk -v begin="$BEGIN" -v end="$END" -v block="$block" '
    $0 == begin { print block; skip = 1; next }
    $0 == end   { skip = 0; next }
    !skip       { print }
' "$README")
current=$(cat "$README")

if [ "$spliced" = "$current" ]; then
    [ $rc -eq 0 ] && echo "README generated block up to date ($(printf '%s\n' "$block" | grep -cE '^\| `') rows)"
    exit $rc
fi
if [ $check -eq 1 ]; then
    echo "!! README STALE: the generated block in tests/repros/README.md does not match tests/repros/*/BASELINE.md"
    echo "   fix: run tests/repros/gen_readme.sh (never edit the block by hand); on a rebase conflict, re-run it instead of resolving the hunk"
    diff <(printf '%s\n' "$current") <(printf '%s\n' "$spliced") | grep -E '^[<>]' | cut -c1-100 | head -20
    exit 1
fi
printf '%s\n' "$spliced" > "$README"
echo "README generated block rewritten ($(printf '%s\n' "$block" | grep -cE '^\| `') rows)"
exit $rc
