#!/bin/bash
# Runs INSIDE dbus-run-session with the sandbox env from run.sh. Starts the
# headless shell, waits for the probe, walks the timeline, kills the shell,
# hands the dumps to verify.py.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
LOG="$RUN/shell.log"
DUMPS="$RUN/dumps"; mkdir -p "$DUMPS"

gsettings set org.gnome.shell disable-user-extensions false
gsettings set org.gnome.shell enabled-extensions "['$UUID', 'speaks-probe@rig']"
gsettings set org.gnome.shell welcome-dialog-last-shown-version '999' 2>/dev/null || true

gnome-shell --headless --virtual-monitor 1280x720 >"$LOG" 2>&1 &
SHELL_PID=$!
die() { echo "!! RIG DID NOT COMPLETE: $*"; kill "$SHELL_PID" 2>/dev/null; grep -iE "gnome-speaks|speaks-probe|JS ERROR|CRITICAL" "$LOG" | tail -30; exit 3; }

probe() { gdbus call --session --dest org.gnome.Speaks.RigProbe --object-path /org/gnome/Speaks/RigProbe --method "org.gnome.Speaks.RigProbe.$@" 2>/dev/null; }
dump() {  # $1 = name, $2 = probe method (default Dump)
    local out
    out=$(probe "${2:-Dump}") || return 1
    # gdbus prints ('<json>',) with the string GVariant-escaped; unwrap it.
    python3 - "$out" >"$DUMPS/$1.json" <<'PY'
import sys, ast, json
s = sys.argv[1].strip()
assert s.startswith("(") and s.endswith(",)"), s[:80]
inner = s[1:-2]
print(json.dumps(json.loads(ast.literal_eval(inner)), indent=1))
PY
}

for i in $(seq 1 60); do
    kill -0 "$SHELL_PID" 2>/dev/null || die "gnome-shell exited early"
    probe Dump >/dev/null && break
    sleep 1
done
probe Dump >/dev/null || die "probe never answered on the bus"
sleep 2   # let bus_watch_name deliver its first verdict and the stage settle

dump t0_no_service || die "dump t0"

python3 "$HERE/stub_service.py" --state listening >"$RUN/stub.log" 2>&1 &
STUB_PID=$!
sleep 3   # name appears (+0s), StateChanged(listening) (+1s), theme settles
dump t1_stub_listening || die "dump t1"

kill "$STUB_PID"; wait "$STUB_PID" 2>/dev/null
sleep 1.5
dump t2_stub_gone || die "dump t2"

probe SetHover true; sleep 0.5; dump t3_hover || die "dump t3"
probe SetHover false; sleep 0.5; dump t4_unhover || die "dump t4"
probe SetFocus true; sleep 0.5; dump t5_focus || die "dump t5"
probe SetFocus false; sleep 0.5; dump t6_unfocus || die "dump t6"

# A tap: systemctl runs against the sandboxed runtime dir and fails.
dump t7_tap_pending TapAndDump || die "dump t7"
sleep 3;  dump t8_tap_failed || die "dump t8"

# The dictation hotkey seam while unavailable, and a non-listen method.
probe Call StartListening; sleep 3; dump t9_hotkey || die "dump t9"
probe Call SpeakClipboard; sleep 0.5; dump t10_other_method || die "dump t10"

# Service comes back idle: leaves the state, menu row flips.
python3 "$HERE/stub_service.py" --state idle >"$RUN/stub2.log" 2>&1 &
STUB_PID=$!
sleep 3
dump t11_stub_idle || die "dump t11"
kill "$STUB_PID"; wait "$STUB_PID" 2>/dev/null
sleep 1.5
dump t12_gone_again || die "dump t12"

# disable / re-enable must be clean
gnome-extensions disable "$UUID"; sleep 1
gnome-extensions enable "$UUID"; sleep 3
dump t13_reenabled || die "dump t13"

kill "$SHELL_PID"; wait "$SHELL_PID" 2>/dev/null
# Belt and braces: with no servicedir nothing should have mounted anything,
# but a FUSE mount left inside the run dir would survive rm -rf.
for m in $(awk -v r="$RUN/" 'index($2, r) == 1 {print $2}' /proc/mounts); do
    fusermount3 -u "$m" 2>/dev/null || fusermount -u "$m" 2>/dev/null || true
done
python3 "$HERE/verify.py" "$DUMPS" "$LOG" "$SRC"
