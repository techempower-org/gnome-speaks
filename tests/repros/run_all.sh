#!/usr/bin/env bash
#
# Run every repro suite against one gnome-speaks-service.py.
#
#   tests/repros/run_all.sh                       # the service in this repo
#   tests/repros/run_all.sh /path/to/service.py   # a worktree, or an extracted SHA
#
# Exits non-zero if any suite fails. One line per check.
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
summarise() {
    _s_out=""
    for _s_pat in '^\[(PASS|FAIL)\]' '^(PASS|FAIL|RESULT):' '^(all checks passed|FAILURES:|ALL )'; do
        _s_out=$(printf '%s\n' "$1" | grep -E "$_s_pat" | tail -1)
        if [ -n "$_s_out" ]; then printf '%s\n' "$_s_out"; return; fi
    done
    printf '%s\n' "$1" | tail -1
}
line() { printf '%-16s %-34s rc=%s  %s\n' "$1" "$2" "$3" "$(summarise "$4" | cut -c1-80)"; }

run() {   # run <suite> <script>...
    suite="$1"; shift
    for f in "$@"; do
        out=$(cd "$HERE/$suite" && timeout 300 python3 "$f" 2>/dev/null); st=$?
        [ $st -ne 0 ] && rc=1
        line "$suite" "$f" "$st" "$out"
    done
}

run service-audit  repro_c1_dispatch_gate.py repro_c2_hold.py repro_c3_config.py \
                   repro_c4_timer.py smoke_queue_ops.py verify_queue_invariants.py
run chronicle-perf repro_chronicle_stall.py verify_archive.py \
                   verify_chronicle_contract.py verify_endpoints.py
run cancel-tokens  repro_a_outcome_mislabel.py repro_b_transcript_after_stop.py \
                   repro_c_streaming_after_stop.py repro_d_mic_disconnect.py \
                   verify_cancel_invariants.py
run subtitle-token repro_e1_stale_complete_frame.py repro_e2_foreign_cancel_freeze.py \
                   repro_e3_streaming_reply_subtitle.py repro_e4_compound_cycle_then_reply.py
run begin-refused  repro_j_first_sentence.py repro_k_remainder.py repro_l_healthy_reply.py
run dead-recorder  repro_e_single_shot_turn_end.py repro_f_text_survives_yank.py \
                   repro_g_ws_init_unbound.py repro_h_stale_prewarm.py \
                   repro_i_healthy_loop.py
run pin-lifecycle  repro_pin_lifecycle.py repro_compound_pin_x_deadmic.py \
                   repro_compound_reply_pin.py
run injector-seam  verify_injector_seam.py verify_ibus_injector.py
run version-cache  verify_version_cache.py repro_a_fork_storm.py

# ---------------------------------------------------------------------------
# offline-handoff: ONE PROCESS PER CASE, on purpose.
#
# (1) A red must name its scenario. This suite caught
#     `[FAIL] D azure-healthy-unchanged` on a trial merge while every other
#     suite was green, because nothing else exercises the ordinary
#     healthy-Azure dictation path; a single rc=1 line would not have said so.
# (2) Case ordering is load bearing. Case H replaces stt.wyoming.transcribe and
#     never restores it, and is safe only because build() re-stubs it on every
#     call; case G restores _rest_stt_fallback explicitly because build() does
#     NOT re-stub that one. A process per case makes this irrelevant instead of
#     merely currently-true.
# ---------------------------------------------------------------------------
for c in A B C D E F G H I; do
    out=$(cd "$HERE/offline-handoff" && timeout 300 python3 repro_offline_handoff.py "$c" 2>/dev/null)
    st=$?; [ $st -ne 0 ] && rc=1
    line offline-handoff "case $c" "$st" "$out"
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
        st=$?; [ $st -ne 0 ] && rc=1
        printf '%-16s %-34s rc=%s  %s\n' prefs-rig "run.sh (gjs/broadway)" "$st" \
            "$(printf '%s\n' "$out" | grep -E 'EXIT_CLEAN|REGRESSION' | tail -1 | cut -c1-80)"
    else
        printf '%-16s %-34s SKIP (no %s:prefs.js baseline)\n' prefs-rig "run.sh" "$ref"
    fi
    rm -rf "$base_dir"
else
    printf '%-16s %-34s SKIP (gjs not installed)\n' prefs-rig "run.sh"
fi

echo
[ $rc -eq 0 ] && echo "ALL SUITES GREEN" || echo "SOME SUITES FAILED"
exit $rc
