#!/usr/bin/env bash
#
# install.sh --check-dropins (#115): a forgotten
# ~/.config/systemd/user/gnome-speaks.service.d/offline.conf carrying
# Environment=SPEECH_FORCE_OFFLINE=1 forced the service offline for weeks
# (#103) and nothing in the install flow said so. The installer now lists every
# such drop-in with a WARN. This is its control, in both directions:
#
#   A  positive control  a planted offline.conf -> WARN names the file AND each
#                        Environment= line, rc=1. Proves the instrument sees
#                        BEFORE any zero below is believed.
#   B  no Environment=   a drop-in with only RestartSec= is still listed
#   C  .conf.disabled    NOT listed, rc=0 -- systemd ignores it, so must we
#   D  silence           no drop-in dir at all -> zero WARN lines, rc=0
#
# Everything runs against a SCRATCH $HOME under this repo's gitignored
# tmp/repros/, created by this process and deleted on exit. The developer's
# real ~/.config is never read or written -- install.sh derives every path
# from $HOME. Only `--check-dropins` is invoked: nothing is installed,
# no systemctl call is made.
#
# Exit contract, shared with tests/repros/run_all.sh:
#   0 clean · 1 the defect is present · 2 SETUP FAILURE
set -u
set -o pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# tests/repros/<suite>/ -> repo root is THREE levels up (the #59 trap: ../.. is tests/).
REPO="$(cd "$HERE/../../.." && pwd)"
INSTALL="${GS_INSTALL_SH:-$REPO/install.sh}"
[ -f "$INSTALL" ] || { echo "!! SETUP FAILURE: no $INSTALL"; exit 2; }
# NEVER hand an installer that does not know the flag: the pre-#115 install.sh
# ignores unknown arguments and performs a FULL INSTALL -- including
# `systemctl --user restart` of the LIVE service, which $HOME does not scope.
# So this suite cannot be re-run against its baseline; discrimination is by
# construction (the flag did not exist), and this guard is what makes that
# safe rather than merely true today.
grep -qF -- '--check-dropins)' "$INSTALL" || {
    echo "!! SETUP FAILURE: $INSTALL has no --check-dropins mode; refusing to run it (it would install)"
    exit 2
}

# /etc is outside $HOME and outside our control: a real drop-in there would make
# case D fail for a reason that is not a defect. Refuse to measure rather than
# report a bogus red (or, worse, weaken D to tolerate it).
if compgen -G "/etc/systemd/user/gnome-speaks.service.d/*.conf" >/dev/null; then
    echo "!! SETUP FAILURE: a system-wide drop-in exists in /etc/systemd/user/gnome-speaks.service.d/"
    exit 2
fi

mkdir -p "$REPO/tmp/repros" 2>/dev/null
SCRATCH=$(mktemp -d "$REPO/tmp/repros/dropins-$$-XXXX" 2>/dev/null) \
    || { echo "!! SETUP FAILURE: cannot create scratch under $REPO/tmp/repros"; exit 2; }
trap 'rm -rf "$SCRATCH"' EXIT INT TERM
FAKE_HOME="$SCRATCH/home"
DROPIN_DIR="$FAKE_HOME/.config/systemd/user/gnome-speaks.service.d"
mkdir -p "$FAKE_HOME"

rc=0
pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*"; rc=1; }
run_check() {   # sets OUT and ST; strips ANSI colour so patterns are literal
    OUT=$(HOME="$FAKE_HOME" "$INSTALL" --check-dropins 2>&1 | sed 's/\x1b\[[0-9;]*m//g')
    ST=${PIPESTATUS[0]}
}
warn_count() { printf '%s\n' "$OUT" | grep -cF '[WARN]' || true; }

# ---- A: positive control ------------------------------------------------
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_DIR/offline.conf" <<'CONF'
[Service]
# forgotten test override
Environment=SPEECH_FORCE_OFFLINE=1
  Environment=GS_PLANTED_SECOND=yes
CONF
run_check
if [ "$ST" -eq 1 ] \
   && printf '%s\n' "$OUT" | grep -qF '[WARN]' \
   && printf '%s\n' "$OUT" | grep -qF "$DROPIN_DIR/offline.conf" \
   && printf '%s\n' "$OUT" | grep -qF 'Environment=SPEECH_FORCE_OFFLINE=1' \
   && printf '%s\n' "$OUT" | grep -qF 'Environment=GS_PLANTED_SECOND=yes' \
   && ! printf '%s\n' "$OUT" | grep -qF 'forgotten test override'; then
    pass "A planted offline.conf -> rc=1, WARN names file + both Environment= lines, comment not echoed"
else
    fail "A planted offline.conf -> rc=$ST ($(warn_count) WARN lines)"
    printf '%s\n' "$OUT" | sed 's/^/      /'
fi

# ---- B: a drop-in with no Environment= is still a drop-in ---------------
rm -f "$DROPIN_DIR/offline.conf"
printf '[Service]\nRestartSec=5\n' > "$DROPIN_DIR/restart.conf"
run_check
if [ "$ST" -eq 1 ] \
   && printf '%s\n' "$OUT" | grep -qF "$DROPIN_DIR/restart.conf" \
   && printf '%s\n' "$OUT" | grep -qF 'no Environment= lines'; then
    pass "B restart.conf (no Environment=) -> rc=1, listed with the no-Environment note"
else
    fail "B restart.conf (no Environment=) -> rc=$ST"
    printf '%s\n' "$OUT" | sed 's/^/      /'
fi

# ---- C: systemd reads *.conf only; so must the check ---------------------
rm -f "$DROPIN_DIR/restart.conf"
printf '[Service]\nEnvironment=SPEECH_FORCE_OFFLINE=1\n' > "$DROPIN_DIR/offline.conf.disabled"
run_check
if [ "$ST" -eq 0 ] && [ "$(warn_count)" -eq 0 ]; then
    pass "C offline.conf.disabled -> rc=0, zero WARN (agrees with systemd)"
else
    fail "C offline.conf.disabled -> rc=$ST ($(warn_count) WARN lines)"
    printf '%s\n' "$OUT" | sed 's/^/      /'
fi

# ---- D: silence when there is nothing to say -----------------------------
rm -rf "$FAKE_HOME/.config"
run_check
if [ "$ST" -eq 0 ] && [ "$(warn_count)" -eq 0 ] \
   && printf '%s\n' "$OUT" | grep -qF '[OK]'; then
    pass "D no drop-in dir -> rc=0, zero WARN, one [OK] line"
else
    fail "D no drop-in dir -> rc=$ST ($(warn_count) WARN lines)"
    printf '%s\n' "$OUT" | sed 's/^/      /'
fi

# ---- containment: nothing outside the scratch dir was created ------------
[ -d "$FAKE_HOME" ] && [ "${FAKE_HOME#"$REPO/tmp/repros/"}" != "$FAKE_HOME" ] \
    && pass "scratch HOME lives under tmp/repros/ and is removed on exit" \
    || fail "scratch HOME escaped tmp/repros/: $FAKE_HOME"

if [ $rc -eq 0 ]; then echo "ALL GREEN"; else echo "FAILURES: see [FAIL] lines above"; fi
exit $rc
