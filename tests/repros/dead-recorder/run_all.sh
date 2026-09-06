#!/usr/bin/env bash
# Run the dead-recorder repro suite against a given gnome-speaks-service.py.
# usage: run_all.sh <path-to-service.py>
#
# BASELINE IS A PINNED SHA, NEVER A BRANCH. These repros were written to fail
# before the #57/#48 fix and pass after it. That claim is only reproducible
# against the commit the fix landed on top of:
#
#   GS_BASELINE_REF=e863b2c     # last commit BEFORE #72 merged (848b855)
#   git archive $GS_BASELINE_REF | tar -x -C <dir> && ./run_all.sh <dir>/gnome-speaks-service.py
#   expected there: e FAIL, f FAIL, h FAIL, g PASS, i PASS
#
# Against a moving ref this check goes vacuous the moment the fix merges: it
# starts comparing the fix to itself and passes forever. Verified 2026-09-06.
set -u
# Default to the service in THIS checkout: tests/repros/<suite>/ -> repo root,
# so a bare run tests the tree you are standing in.
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
SVC="${1:-$REPO/gnome-speaks-service.py}"
cd "$(dirname "$0")" || exit 2
rc=0
for r in repro_e_single_shot_turn_end.py repro_f_text_survives_yank.py \
         repro_g_ws_init_unbound.py repro_h_stale_prewarm.py \
         repro_i_healthy_loop.py; do
  out=$(GS_SVC_PATH="$SVC" timeout 120 python3 "$r" 2>/dev/null)
  st=$?
  verdict=$(printf '%s\n' "$out" | grep -E '^(PASS|FAIL):' | tail -1)
  [ $st -ne 0 ] && rc=1
  printf '%-38s rc=%s  %s\n' "$r" "$st" "$verdict"
done
exit $rc
