#!/bin/bash
# Build a REAL mapped Adw.PreferencesWindow from any prefs.js and report on the
# Audio page. Uses gtk4-broadwayd, so it never touches the live GNOME session:
# no window appears, no focus is stolen, safe while someone is dictating.
#
#   ./run.sh <abs-path-to-prefs.js>            # test one file
#   ./run.sh <branch-prefs.js> <main-prefs.js> # test + baseline + stderr diff
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
HERE=$(cd "$(dirname "$0")" && pwd)
BRANCH=${1:?usage: run.sh <prefs.js> [baseline-prefs.js]}
BASE=${2:-}
EXT=$(dirname "$BRANCH")

RUN="$HERE/run-$$"
BWPID=""
cleanup() { [ -n "$BWPID" ] && kill "$BWPID" 2>/dev/null; rm -rf "$RUN"; }
trap cleanup EXIT
mkdir -p "$RUN/home/.config/speech-to-cli" "$RUN/fakeext/schemas"

glib-compile-schemas --targetdir "$RUN/fakeext/schemas" "$EXT/schemas/" || exit 1
cp "$EXT/metadata.json" "$RUN/fakeext/" || exit 1

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
    [ -S "$sock" ] && continue
    gtk4-broadwayd ":$n" >"$RUN/broadwayd.log" 2>&1 &
    BWPID=$!
    for _ in $(seq 1 400); do [ -S "$sock" ] && break; done
    if [ -S "$sock" ]; then DISP=":$n"; break; fi
    kill "$BWPID" 2>/dev/null; BWPID=""
done
[ -n "$DISP" ] || { echo "no free broadway display; see $RUN/broadwayd.log" >&2; exit 1; }
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
        *) echo "!! harness DID NOT COMPLETE for $1 -- stderr below, comparison is meaningless" >&2
           cat "$2" >&2; return 1 ;;
    esac
}

if [ -n "$BASE" ]; then
    echo "=== BASELINE $BASE ==="; run "$BASE" "$RUN/base.stderr" || exit 1
fi
echo "=== BRANCH $BRANCH ==="; run "$BRANCH" "$RUN/branch.stderr" || exit 1
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
        echo "!! REGRESSION: branch emits more warnings than baseline ($br > $bl)" >&2
        rc=1
    fi
fi
exit $rc
