#!/bin/bash
# Build a REAL mapped Adw.PreferencesWindow from any prefs.js and report on the
# Audio page. Uses gtk4-broadwayd, so it never touches the live GNOME session:
# no window appears, no focus is stolen, safe while someone is dictating.
#
#   ./run.sh <prefs.js>                          # test one file
#   ./run.sh <prefs.js> <baseline-prefs.js>      # + baseline + stderr diff
#   ./run.sh <prefs.js> --baseline-rev <sha>     # baseline straight from git
#
# Relative paths are fine -- they are resolved here, while the caller's cwd is
# still known. --baseline-rev extracts into the managed run dir, so there is no
# user-supplied path to point at a file you did not mean to overwrite.
#
# Why this exists: `gnome-extensions prefs <uuid>` loads the INSTALLED copy in
# ~/.local/share/gnome-shell/extensions, not your worktree, so it cannot verify
# an uncommitted prefs.js change. This can.
#
# ISOLATION CONTRACT (do not weaken any of these):
#   - Everything a run writes lives under $HERE/run-$$ and is deleted on exit.
#     Nothing is at a fixed path, so two concurrent rigs cannot share state.
#   - The config.json handed to prefs.js is a RIG-OWNED FIXTURE with pinned
#     keys. JP's ~/.config/speech-to-cli/config.json is NEVER read: a live
#     config steering a verdict is how a rig starts lying.
#   - HOME and GSETTINGS_BACKEND are exported into the gjs child only, never
#     into this shell, so a run cannot write the real config or dconf.
#   - The broadway display number is per-PID and probed until one binds.
set -u
# Prophylactic: this script has no pipeline whose exit status is consumed today,
# so pipefail changes nothing here YET. It is set so that the next pipeline added
# cannot silently inherit `... | grep X | tail -1`, which returns 0 when the grep
# matched nothing.
set -o pipefail

# ⚠️ Deliberately NOT `set -e`, and this part is NOT prophylactic -- it is a live
# hazard. `grep -c` exits 1 when the count is 0, and count 0 is ZERO WARNINGS,
# i.e. the PASSING case. Measured: `set -euo pipefail` aborts this script on the
# two grep -c lines below precisely when a run succeeds. Anyone who reads the
# pipeline-status section in tests/repros/README.md and reaches for the standard
# hardening will break this script; write counts as `$(grep -c X f || true)`
# first if you ever add it.

# EXIT CONTRACT -- assert the LINE, not just the code:
#   0  both runs completed, branch emits no more warnings than baseline
#   1  REGRESSION      -> "!! REGRESSION: ..."       (stdout)
#   2  SETUP FAILURE   -> "!! SETUP FAILURE: ..."    (stdout)
#   3  HARNESS DID NOT COMPLETE -> "!! HARNESS ..."  (stdout)
# 1 is reserved strictly for a real regression, so a red meaning "this machine
# was busy" can never be read as "your change broke something". All verdict
# lines go to STDOUT: they are the result, not a diagnostic, and a caller
# capturing only stdout must still see WHY it failed.
#
# Defined here, above its first use: as a function called via `|| setup_fail`,
# a definition further down silently becomes a 127 that `||` swallows, and the
# script runs on past the precondition it was supposed to stop at.
setup_fail() { echo "!! SETUP FAILURE: $*"; exit 2; }

HERE=$(cd "$(dirname "$0")" && pwd)
BRANCH=${1:?usage: run.sh <prefs.js> [baseline-prefs.js | --baseline-rev <sha>]}

# Resolve while the caller's cwd is still ours: harness.js needs an absolute
# path (a module URI resolves relative paths against the harness's own dir) and
# everything below runs after a `cd`.
abspath() { case "$1" in /*) printf '%s\n' "$1" ;; *) printf '%s\n' "$PWD/$1" ;; esac; }
BRANCH=$(abspath "$BRANCH")
command -v gjs            >/dev/null 2>&1 || setup_fail "gjs not installed"
command -v gtk4-broadwayd >/dev/null 2>&1 || setup_fail "gtk4-broadwayd not installed"
[ -f "$BRANCH" ] || setup_fail "no such file: $BRANCH"

BASE=""
BASE_REV=""
if [ "${2:-}" = "--baseline-rev" ]; then
    BASE_REV=${3:?--baseline-rev needs a git revision}
elif [ -n "${2:-}" ]; then
    BASE=$(abspath "$2")
    [ -f "$BASE" ] || setup_fail "no such baseline file: $BASE"
fi

EXT=$(dirname "$BRANCH")

RUN="$HERE/run-$$"
BWPID=""
OURSOCK=""

# A killed gtk4-broadwayd LEAVES ITS SOCKET FILE BEHIND. Treating "file exists"
# as "display occupied" meant every run permanently burned a display number --
# 62 stale sockets had accumulated here, against a probe window only 26 wide, so
# the rig got monotonically more likely to fail the longer it was used. That is
# the real flake, and it is why it showed up during a repeated hunt.
# broadwayd rebinds over a stale file happily (measured), so the only correct
# question is whether something is LISTENING.
sock_live() { timeout 1 python3 -c \
    "import socket,sys; socket.socket(socket.AF_UNIX).connect(sys.argv[1])" "$1" 2>/dev/null; }
# Keep $RUN on any non-zero exit so broadwayd.log survives for exactly the case
# where someone needs it; clean up only on success. A red that deletes its own
# evidence is the red people learn to skim.
cleanup() {
    _rc=$?
    [ -n "$BWPID" ] && kill "$BWPID" 2>/dev/null
    [ -n "$OURSOCK" ] && rm -f "$OURSOCK"     # do not leak a display number
    if [ "$_rc" -eq 0 ]; then rm -rf "$RUN"
    else echo "run dir kept for diagnosis: $RUN"; fi
}
trap cleanup EXIT
mkdir -p "$RUN/home/.config/speech-to-cli" "$RUN/fakeext/schemas"

# --baseline-rev: extract into the run dir, so no caller-supplied path is
# written to and nothing survives the run.
if [ -n "$BASE_REV" ]; then
    BASE="$RUN/baseline-prefs.js"
    git -C "$EXT" show "$BASE_REV:prefs.js" > "$BASE" 2>/dev/null \
        || setup_fail "could not extract prefs.js at '$BASE_REV' from $EXT"
    echo "BASELINE from git rev $BASE_REV"
fi

glib-compile-schemas --targetdir "$RUN/fakeext/schemas" "$EXT/schemas/" \
    || setup_fail "glib-compile-schemas failed for $EXT/schemas/"
cp "$EXT/metadata.json" "$RUN/fakeext/" || setup_fail "no metadata.json in $EXT"

# ── Rig-owned fixture ────────────────────────────────────────────────────────
# Pinned keys only. Device ids are derived from THIS machine's live wpctl so the
# "config names a device" path is exercised anywhere, not just where node 63
# happens to exist -- still rig-owned, since it never touches JP's config.
python3 - "$RUN" <<'PY'
import json, re, subprocess, sys

def first_ids():
    try:
        out = subprocess.run(['wpctl', 'status'], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return None, None
    found, in_audio, section = {}, False, None
    for line in out.split('\n'):
        t = line.strip()
        if t == 'Audio':
            in_audio = True; continue
        if in_audio and re.match(r'^(Video|Settings)$', t):
            break
        if not in_audio:
            continue
        if t.endswith('Sinks:'):
            section = 'sink'; continue
        if t.endswith('Sources:'):
            section = 'source'; continue
        if section:
            m = re.match(r'^(\*?)\s*(\d+)\.\s+(.+?)(?:\s+\[.*\])?\s*$',
                         re.sub(r'^[│├└─┬┤┼╌╎\s]+', '', t))
            if m:
                found.setdefault(section, m.group(2))
    return found.get('sink'), found.get('source')

sink, source = first_ids()
cfg = {                      # pinned -- the ONLY things steering a verdict
    'player': 'aplay',
    'recorder': 'pw-record',
    'half_duplex': 'auto',
    'enable_echo_cancel': False,
    'enable_pause': True,
    'debug': False,
}
if sink:
    cfg['speaker_sink'] = sink
if source:
    cfg['mic_source'] = source
json.dump(cfg, open(sys.argv[1] + '/home/.config/speech-to-cli/config.json', 'w'),
          indent=2)
print(f"FIXTURE rig-owned, {len(cfg)} pinned keys "
      f"(speaker_sink={cfg.get('speaker_sink')} mic_source={cfg.get('mic_source')} "
      f"from live wpctl); JP's config NOT read")
PY

# ── Per-PID broadway display ─────────────────────────────────────────────────
DISP=""
start=$(( 20 + ($$ % 60) ))
for n in $(seq $start $((start + 25))); do
    sock="/run/user/$(id -u)/broadway$((n + 1)).socket"   # display :N -> socket N+1
    sock_live "$sock" && continue                          # genuinely in use
    gtk4-broadwayd ":$n" >"$RUN/broadwayd.log" 2>&1 &
    BWPID=$!
    # Wait in WALL-CLOCK, not iterations. The previous bound was 400 shell
    # iterations, measured at 5 ms -- against a socket that takes 4 ms to
    # appear on an idle machine. A 1 ms margin, so any concurrent load made
    # this give up, kill a perfectly good broadwayd, walk all 26 displays and
    # report "no free broadway display". That was the seq-3 flake.
    # 5 s is ~1000x the measured 4 ms bind and still returns the instant the
    # socket appears.
    _deadline=$(( $(date +%s) + 5 ))
    while ! sock_live "$sock" && [ "$(date +%s)" -lt "$_deadline" ]; do sleep 0.05; done
    if sock_live "$sock"; then DISP=":$n"; OURSOCK="$sock"; break; fi
    kill "$BWPID" 2>/dev/null; BWPID=""
done
[ -n "$DISP" ] || setup_fail "no free broadway display in :$start..:$((start+25)); see $RUN/broadwayd.log"
echo "DISPLAY broadway $DISP (per-PID), run dir $RUN"

# A run that CRASHED also produces "no new warnings". Require the harness to
# have actually reached the end before any stderr comparison is believed --
# a clean diff between two crashes is not a passing test.
run() {   # run <prefs.js> <stderr-file>
    local out
    out=$( cd "$RUN" && GI_TYPELIB_PATH=/usr/lib/gnome-shell/girepository-1.0 \
      LD_LIBRARY_PATH=/usr/lib/gnome-shell HOME="$RUN/home" \
      GS_PREFS_EXTDIR="$RUN/fakeext" \
      GSETTINGS_BACKEND=memory GDK_BACKEND=broadway BROADWAY_DISPLAY=$DISP \
      timeout 60 gjs -m "$HERE/harness.js" "$1" 2>"$2" )
    echo "$out"
    case "$out" in
        *EXIT_CLEAN*) return 0 ;;
        *) echo "!! HARNESS DID NOT COMPLETE for $1 -- comparison is meaningless"
           cat "$2" >&2; return 3 ;;
    esac
}

if [ -n "$BASE" ]; then
    echo "=== BASELINE $BASE ==="; run "$BASE" "$RUN/base.stderr" || exit 3
fi
echo "=== BRANCH $BRANCH ==="; run "$BRANCH" "$RUN/branch.stderr" || exit 3
echo "--- stderr ---"; cat "$RUN/branch.stderr"
rc=0
if [ -n "$BASE" ]; then
    bl=$(grep -c 'Gtk-WARNING\|Adw-WARNING' "$RUN/base.stderr")
    br=$(grep -c 'Gtk-WARNING\|Adw-WARNING' "$RUN/branch.stderr")
    echo "=== Gtk/Adw warning counts ==="
    printf 'baseline=%s  branch=%s\n' "$bl" "$br"
    echo "=== stderr delta vs baseline ('<' = fixed, '>' = INTRODUCED) ==="
    scrub() { sed 's/gjs:[0-9]*//; s/[0-9][0-9]:[0-9][0-9]:[0-9][0-9]\.[0-9]*//' "$1"; }
    diff <(scrub "$RUN/base.stderr") <(scrub "$RUN/branch.stderr") \
        && echo "NONE — stderr identical to baseline"
    # EXIT CONTRACT: the diff exiting non-zero is NOT failure -- a fix is
    # SUPPOSED to change stderr. Only a regression or an incomplete run fails.
    if [ "$br" -gt "$bl" ]; then
        echo "!! REGRESSION: branch emits more warnings than baseline ($br > $bl)"
        rc=1
    fi
fi
exit $rc
