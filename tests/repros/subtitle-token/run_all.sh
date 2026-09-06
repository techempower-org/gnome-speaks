#!/usr/bin/env bash
# Usage: ./run_all.sh /path/to/gnome-speaks-service.py
set -u
# Default to the service in THIS checkout: tests/repros/<suite>/ -> repo root.
# A hardcoded absolute path is both a public-repo leak and a broken contract --
# it silently tests some other tree than the one the caller is standing in.
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
SVC="${1:-$REPO/gnome-speaks-service.py}"
cd "$(dirname "$0")" || exit 2
rc=0
for f in repro_e1_stale_complete_frame.py repro_e2_foreign_cancel_freeze.py repro_e3_streaming_reply_subtitle.py repro_e4_compound_cycle_then_reply.py; do
    echo "=== $f ==="
    GS_SVC_PATH="$SVC" python3 "$f"; s=$?
    echo "--- exit $s"
    [ $s -ne 0 ] && rc=$s
done
exit $rc
