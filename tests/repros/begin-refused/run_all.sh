#!/usr/bin/env bash
# Usage: ./run_all.sh /path/to/gnome-speaks-service.py
set -u
# Default to the service in THIS checkout: tests/repros/<suite>/ -> repo root,
# so a bare run tests the tree you are standing in.
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
SVC="${1:-$REPO/gnome-speaks-service.py}"
cd "$(dirname "$0")" || exit 2
rc=0
for f in repro_j_first_sentence.py repro_k_remainder.py repro_l_healthy_reply.py; do
    echo "=== $f ==="
    GS_SVC_PATH="$SVC" python3 "$f"; s=$?
    echo "--- exit $s"
    [ $s -ne 0 ] && rc=$s
done
exit $rc
