#!/usr/bin/env bash
#
# Run every repro suite against one gnome-speaks-service.py.
#
#   tests/repros/run_all.sh                       # the service in this repo
#   tests/repros/run_all.sh /path/to/service.py   # a worktree, or an extracted SHA
#
# Exits non-zero if any suite fails. One line per check.
#
# Suites are DISCOVERED from tests/repros/<suite>/suite.list (#169) -- adding a
# suite never edits this file. See the DISCOVERY block below and
# tests/repros/README.md, "Adding a suite".
#
# NO TEST FRAMEWORK, by project rule: these are plain scripts that exit 0 (clean),
# 1 (the defect is present) or 2 (SETUP FAILURE -- the repro could not create the
# window it needed, so it is reporting neither a pass nor a bug).
#
# ENV CONTRACT: GS_SVC_PATH -- the service file under test -- is the only input,
# and this runner sets it. The worktree dir (for sibling modules) and the scratch
# dir are DERIVED. GS_WT overrides the dir only to mix trees deliberately.
#
# SERIAL, on purpose. Scratch is keyed by PID so concurrent agents no longer
# corrupt each other, but several suites are timing-sensitive (silence timeouts,
# audio-level throttles, pipe back-pressure) and overlapping runs make their
# measurements unreliable.
set -u
# pipefail so a pipeline's status reflects the interesting command, not just the
# last one. Prophylactic here -- no pipeline in this file has its status consumed
# today -- and set uniformly across all five runners so the next one added cannot
# inherit `... | grep X | tail -1`, which returns 0 when the grep matched nothing.
#
# ⚠️ NOT `set -e`, deliberately: `grep -c` exits 1 on a zero count, and a zero
# count is the PASSING case for a warning counter. `set -euo pipefail` aborts on
# success. If you add -e, write every count as `$(grep -c X f || true)` first.
set -o pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
SVC="${1:-$REPO/gnome-speaks-service.py}"
[ -f "$SVC" ] || { echo "no such service file: $SVC" >&2; exit 2; }
# Resolve while the caller's cwd is still ours. The check above validates HERE,
# but every suite runs via `cd "$HERE/$suite"` and resolves GS_SVC_PATH THERE --
# so a relative path passes validation and then fails every suite with
# FileNotFoundError naming a path inside the suite directory.
case "$SVC" in /*) ;; *) SVC="$PWD/$SVC" ;; esac
export GS_SVC_PATH="$SVC"
rc=0

# Summarise a suite's stdout. Order matters: a per-case "[PASS] D ..." line is
# more informative than the trailing "ALL GREEN", and a red must name its
# scenario, so bracketed case lines win over the roll-up.
#
# NOTE the shape here: each pattern's result is captured and TESTED FOR
# CONTENT, not chained with `grep ... | tail -1 && ...`. In that form `tail`
# exits 0 on empty input, so the first pattern always "succeeds" and every
# summary comes out blank -- the same pipeline-exit-status trap that makes a
# script ending in `diff` return 1 on success.
#
# EXIT_CLEAN is prefs-rig's own "the run completed" marker and belongs with
# the other roll-up verdicts; without it that line falls through to the
# generic tail -1 and reports the stderr-delta line instead of the verdict.
#
# `^!!` is FIRST on purpose. A suite emits `!! SETUP FAILURE` / `!! REGRESSION`
# / `!! HARNESS DID NOT COMPLETE` for the things a reader must not miss, and a
# run can limp as far as a `[PASS]` line and THEN fail setup -- if the case
# lines won, that run would report the pass.
summarise() {   # summarise <output> <label> <rc>
    _s_out=""
    for _s_pat in '^!!' '^\[(PASS|FAIL)\]' '^(PASS|FAIL|RESULT):' \
                  '^(all checks passed|FAILURES:|ALL |EXIT_CLEAN)'; do
        _s_out=$(printf '%s\n' "$1" | grep -E "$_s_pat" | tail -1)
        if [ -n "$_s_out" ]; then printf '%s\n' "$_s_out"; return; fi
    done
    _s_out=$(printf '%s\n' "$1" | tail -1)
    if [ -n "$_s_out" ]; then printf '%s\n' "$_s_out"; return; fi
    # THE INVARIANT: a non-zero rc must never print a blank diagnostic. The
    # fallback above is `tail -1`, which returns EMPTY on empty input -- so
    # without this line "always prints a diagnostic" was only mostly true, and
    # a red with nothing after it is the red people learn to skim.
    printf '!! NO OUTPUT from %s (rc=%s)\n' "$2" "$3"
}

# Exit codes are a contract, and the summary counts them separately so "green"
# means exactly one thing: 0 clean · 1 the defect is present · 2 SETUP FAILURE
# (the check could not run, so it reports neither a pass nor a bug) · 3 the
# harness did not complete. 2 and 3 still fail the gate -- a gate that goes
# green on a check that never executed is measuring the absence of the test --
# but they are labelled and counted apart from a real red.
n_pass=0; n_fail=0; n_setup=0; n_incomplete=0
tally() {
    case "$1" in
        0) n_pass=$((n_pass + 1)) ;;
        2) n_setup=$((n_setup + 1)); rc=1 ;;
        3) n_incomplete=$((n_incomplete + 1)); rc=1 ;;
        *) n_fail=$((n_fail + 1)); rc=1 ;;
    esac
}
line() {
    case "$3" in
        0) _l_tag="" ;;
        2) _l_tag="  [SETUP FAILURE]" ;;
        3) _l_tag="  [HARNESS INCOMPLETE]" ;;
        *) _l_tag="" ;;
    esac
    tally "$3"
    printf '%-16s %-34s rc=%s%s  %s\n' "$1" "$2" "$3" "$_l_tag" \
        "$(summarise "$4" "$1/$2" "$3" | cut -c1-80)"
}

# ---------------------------------------------------------------------------
# DISCOVERY (#169). Suites are collected from tests/repros/<suite>/suite.list,
# in LC_ALL=C order of the directory name, so lanes adding suites touch DISJOINT
# files -- the hand-maintained `run <suite> ...` list here conflicted on every
# concurrent PR. No order file: every suite is its own process with per-PID
# scratch and none depends on another (verified 2026-09-08 by diffing the
# per-check verdict lines of the hand-ordered runner against this one).
#
# suite.list: one CHECK per line, in run order. Blank lines and `#` comments
# are skipped. Two shapes:
#     repro_x.py                     label = the script; python3 repro_x.py
#     case A: repro_x.py A           label "case A"; python3 repro_x.py A
# i.e. an optional `label: ` prefix (first ": "), then the command, which is
# split on whitespace (no quoting). The first token is resolved relative to the
# suite directory and the check runs WITH THAT DIRECTORY AS CWD. A `*.py` token
# runs under python3 with stderr discarded and a 300 s timeout (the shape the
# python suites always had); anything else is executed directly with stderr
# merged and a 60 s timeout (the shape leak-scan and install-dropins had).
#
# ONE PROCESS PER CASE (offline-handoff, wake-watcher) is expressed as one line
# per case -- the reasons are in each suite.list, next to the lines they govern.
#
# The manifest/README check runs FIRST: gen_readme.sh --check fails when a
# suite directory has no suite.list (the runner would silently skip it -- a
# gate green on a test that never ran), no BASELINE.md, or when the generated
# table in README.md is stale. Its fix is always "run tests/repros/gen_readme.sh".
# ---------------------------------------------------------------------------
out=$(timeout 60 "$HERE/gen_readme.sh" --check 2>&1); st=$?
line manifests "gen_readme.sh --check" "$st" "$out"

for list in $(cd "$HERE" && ls -d -- */suite.list 2>/dev/null | LC_ALL=C sort); do
    suite="${list%/suite.list}"
    while IFS= read -r raw || [ -n "$raw" ]; do
        # strip comments and surrounding whitespace
        raw="${raw%%#*}"
        raw="$(printf '%s' "$raw" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        [ -n "$raw" ] || continue
        case "$raw" in
            *": "*) label="${raw%%: *}"; cmd="${raw#*: }" ;;
            *)      label="$raw";        cmd="$raw" ;;
        esac
        # shellcheck disable=SC2086  -- word splitting is the contract
        set -- $cmd
        first="$1"; shift
        case "$first" in
            *.py) out=$(cd "$HERE/$suite" && timeout 300 python3 "$first" "$@" 2>/dev/null); st=$? ;;
            *)    out=$(cd "$HERE/$suite" && timeout 60 "$HERE/$suite/$first" "$@" 2>&1); st=$? ;;
        esac
        line "$suite" "$label" "$st" "$out"
    done < "$HERE/$list"
done

# ---------------------------------------------------------------------------
# prefs-rig is GJS/bash, not python: run.sh -> gjs + gtk4-broadwayd. A *.py
# inventory returns a false negative on it and a python collector must never
# import it. It needs a BASELINE prefs.js to mean anything, so we hand it the
# merge base rather than a moving origin/main -- a fast-moving main manufactures
# false regressions.
#
# Its exit contract is its own and is NOT the usual one: 0 = both runs
# completed; 1 = a run did not complete, or the branch emits MORE warnings than
# the baseline. A DIFFERING stderr is not a failure -- a fix is supposed to
# change stderr.
# ---------------------------------------------------------------------------
if [ -x "$HERE/prefs-rig/run.sh" ] && command -v gjs >/dev/null 2>&1; then
    base_dir=$(mktemp -d "$REPO/tmp/prefs-baseline-$$-XXXX" 2>/dev/null) \
        || base_dir=$(mktemp -d)
    ref=$(git -C "$REPO" merge-base origin/main HEAD 2>/dev/null || echo origin/main)
    if git -C "$REPO" show "$ref:prefs.js" > "$base_dir/prefs.js" 2>/dev/null; then
        out=$(cd "$HERE/prefs-rig" && timeout 300 ./run.sh "$REPO/prefs.js" "$base_dir/prefs.js" 2>&1)
        st=$?
        # Through line()/summarise() like every other suite. This branch used
        # to hand-roll `grep -E 'EXIT_CLEAN|REGRESSION' | tail -1`, which
        # matched neither a setup failure nor anything else the rig says when
        # it cannot start -- so the one check that actually flaked reported
        # `rc=1` followed by nothing at all. It was the only branch written by
        # hand instead of using the fix three lines above it.
        line prefs-rig "run.sh (gjs/broadway)" "$st" "$out"
    else
        printf '%-16s %-34s SKIP (no %s:prefs.js baseline)\n' prefs-rig "run.sh" "$ref"
    fi
    rm -rf "$base_dir"
else
    printf '%-16s %-34s SKIP (gjs not installed)\n' prefs-rig "run.sh"
fi

echo
n_total=$((n_pass + n_fail + n_setup + n_incomplete))
_sum="$n_total checks: $n_pass pass, $n_fail fail"
[ "$n_setup" -gt 0 ] && _sum="$_sum, $n_setup SETUP FAILURE"
[ "$n_incomplete" -gt 0 ] && _sum="$_sum, $n_incomplete HARNESS INCOMPLETE"
echo "$_sum"
[ $rc -eq 0 ] && echo "ALL SUITES GREEN" || echo "SOME SUITES FAILED"
exit $rc
