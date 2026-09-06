#!/usr/bin/env bash
# Usage: ./run_all.sh /path/to/gnome-speaks-service.py
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
