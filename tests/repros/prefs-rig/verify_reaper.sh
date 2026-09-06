#!/usr/bin/env bash
#
# Control for #94: orphaned broadwayds must not exhaust the probe window.
#
#   ./verify_reaper.sh
#
# THIS CONTROL CAN FAIL, and that is the whole point of it. The previous
# candidate control for the sibling socket bug -- two concurrent run.sh, count
# sockets before and after -- returned 0 -> 0 against UNFIXED code, so it would
# have passed on the bug it was meant to catch. A gate that is green on broken
# code manufactures confidence. This one is verified in both directions:
#
#   with the reaper     the rig finds a display   -> PASS
#   reaper disabled     the rig cannot            -> the control goes RED
#
# It fills a deterministic 26-display window with LIVE orphans (broadwayd
# processes whose run-<pid>/broadwayd.log has been deleted), which is what a
# SIGKILLed run leaves behind. A live orphan is not stale -- sock_live() reads
# it as "occupied" and skips it forever.
#
# Not wired into run_all.sh: it starts 26 processes and is a verification of
# the reaper, not of the service. Run it when touching the probe or the reaper.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
UID_="$(id -u)"
START=200                      # far outside the default 20..104 window
N=26                           # the whole probe span
PIDS=()
ORPHAN_DIR=$(mktemp -d)

cleanup() {
    for p in ${PIDS+"${PIDS[@]}"}; do kill "$p" 2>/dev/null; done
    for n in $(seq $START $((START + N - 1))); do rm -f "/run/user/$UID_/broadway$((n + 1)).socket"; done
    rm -rf "$ORPHAN_DIR"
}
trap cleanup EXIT

make_orphans() {
    for n in $(seq $START $((START + N - 1))); do
        d="$ORPHAN_DIR/run-$n"; mkdir -p "$d"
        gtk4-broadwayd ":$n" >"$d/broadwayd.log" 2>&1 &
        PIDS+=($!)
    done
    sleep 2                                   # let them all bind
    rm -rf "$ORPHAN_DIR"/run-*                # the run dirs are GONE: orphans
}

live=0
for n in $(seq $START $((START + N - 1))); do
    timeout 1 python3 -c "import socket,sys; socket.socket(socket.AF_UNIX).connect(sys.argv[1])" \
        "/run/user/$UID_/broadway$((n + 1)).socket" 2>/dev/null && live=$((live + 1))
done
[ "$live" -eq 0 ] || { echo "!! SETUP FAILURE: displays :$START..$((START+N-1)) already in use ($live)"; exit 2; }

echo "== creating $N live orphans on :$START..:$((START + N - 1)) =="
make_orphans
live=0
for n in $(seq $START $((START + N - 1))); do
    timeout 1 python3 -c "import socket,sys; socket.socket(socket.AF_UNIX).connect(sys.argv[1])" \
        "/run/user/$UID_/broadway$((n + 1)).socket" 2>/dev/null && live=$((live + 1))
done
echo "   live orphans holding displays: $live/$N"
[ "$live" -eq "$N" ] || { echo "!! SETUP FAILURE: only $live/$N orphans bound; cannot test exhaustion"; exit 2; }

BASE=$(mktemp -d)
git -C "$REPO" show "$(git -C "$REPO" merge-base origin/main HEAD)":prefs.js > "$BASE/prefs.js" 2>/dev/null \
    || { echo "!! SETUP FAILURE: no baseline prefs.js"; rm -rf "$BASE"; exit 2; }

echo "== A. reaper DISABLED -- the control must go RED here =="
GS_BROADWAY_START=$START GS_REAP_DISABLE=1 timeout 300 "$HERE/run.sh" "$REPO/prefs.js" "$BASE/prefs.js" >"$ORPHAN_DIR.a" 2>&1
a=$?
echo "   rc=$a  $(grep -m1 '^!!' "$ORPHAN_DIR.a" 2>/dev/null || echo '(no !! line)')"

echo "== B. reaper ENABLED -- the rig must find a display =="
GS_BROADWAY_START=$START timeout 300 "$HERE/run.sh" "$REPO/prefs.js" "$BASE/prefs.js" >"$ORPHAN_DIR.b" 2>&1
b=$?
echo "   rc=$b  $(grep -cE '^reaped orphaned broadwayd' "$ORPHAN_DIR.b" 2>/dev/null || echo 0) orphans reaped"
rm -rf "$BASE"

echo
if [ "$a" -eq 0 ]; then
    echo "!! CONTROL IS MEANINGLESS: the rig succeeded with the reaper DISABLED."
    echo "   A control that cannot fail proves nothing. Fix the control, not the code."
    rc=1
elif [ "$b" -ne 0 ]; then
    echo "!! REAPER DID NOT RECOVER THE WINDOW (rc=$b)"; sed -n '1,20p' "$ORPHAN_DIR.b"
    rc=1
else
    echo "PASS: disabled -> rc=$a (window exhausted); enabled -> rc=0 (orphans reaped)"
    rc=0
fi
rm -f "$ORPHAN_DIR.a" "$ORPHAN_DIR.b"
exit $rc
