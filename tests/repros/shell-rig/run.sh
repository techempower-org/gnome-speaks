#!/bin/bash
# Load a checkout's extension.js + stylesheet.css into a REAL headless
# gnome-shell on a private session bus and measure the badge from inside
# (computed St.ThemeNode colours, accessible name, label visibility, panel
# menu rows, notifications) while org.gnome.Speaks is absent, present, and
# gone again. `--nested` is dead on GNOME 50; this is the replacement rig the
# "St CSS: measure, don't reason" gotcha asks for.
#
#   ./run.sh                    # the checkout this script lives in
#   ./run.sh /path/to/checkout  # a worktree, or an extracted SHA
#
# ISOLATION CONTRACT (do not weaken any of these):
#   - Everything lives under $HERE/run-$$ and is deleted on exit. No fixed
#     paths, so two rigs can run at once.
#   - HOME, XDG_{DATA,CONFIG,CACHE,RUNTIME}_HOME/DIR and GSETTINGS_BACKEND=
#     keyfile are exported into the dbus-run-session child ONLY. The shell
#     never sees the real dconf, the real extensions dir, or the real
#     Wayland display (DISPLAY/WAYLAND_DISPLAY are unset).
#   - XDG_RUNTIME_DIR is sandboxed, so `systemctl --user` spawned by the
#     badge CANNOT reach the real user manager: it fails to connect, which
#     is exactly the failure path the rig wants to see toasted. The control
#     below proves that before the shell starts; if it ever succeeds, the
#     run is aborted rather than risk starting or touching the real unit.
#   - The bus is dbus-run-session's own, so the stub owning org.gnome.Speaks
#     never collides with the running service.
#
# EXIT CONTRACT -- assert the LINE, not just the code:
#   0  every check passed and the shell logged no gnome-speaks errors
#   1  REGRESSION            -> "!! FAIL ..." lines from verify.py
#   2  SETUP FAILURE         -> "!! SETUP FAILURE: ..."
#   3  RIG DID NOT COMPLETE  -> "!! RIG ..." (shell never came up, probe unreachable)
set -u
set -o pipefail
setup_fail() { echo "!! SETUP FAILURE: $*"; exit 2; }
rig_fail()   { echo "!! RIG DID NOT COMPLETE: $*"; exit 3; }

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${1:-$(cd "$HERE/../../.." && pwd)}
SRC=$(cd "$SRC" 2>/dev/null && pwd) || setup_fail "no such checkout: ${1:-}"
for f in extension.js stylesheet.css metadata.json schemas/org.gnome.shell.extensions.gnome-speaks.gschema.xml; do
    [ -f "$SRC/$f" ] || setup_fail "$SRC/$f missing"
done
for bin in gnome-shell dbus-run-session gdbus gsettings glib-compile-schemas python3; do
    command -v "$bin" >/dev/null || setup_fail "$bin not on PATH"
done

RUN="$HERE/run-$$"
cleanup() {
    # Unmount anything a child left under the run dir before removing it.
    for m in $(awk -v r="$RUN/" 'index($2, r) == 1 {print $2}' /proc/mounts 2>/dev/null); do
        fusermount3 -u "$m" 2>/dev/null || fusermount -u "$m" 2>/dev/null || true
    done
    rm -rf "$RUN"
}
trap cleanup EXIT
mkdir -p "$RUN"/{home,data,config,cache} || setup_fail "mkdir $RUN"
mkdir -m 700 "$RUN/runtime" || setup_fail "mkdir runtime"

UUID=gnome-speaks@jphein
EXT="$RUN/data/gnome-shell/extensions/$UUID"
mkdir -p "$EXT/schemas"
cp "$SRC/extension.js" "$SRC/stylesheet.css" "$SRC/metadata.json" "$EXT/"
cp "$SRC/schemas/org.gnome.shell.extensions.gnome-speaks.gschema.xml" "$EXT/schemas/"
glib-compile-schemas "$EXT/schemas/" || setup_fail "schema compile"
PROBE="$RUN/data/gnome-shell/extensions/speaks-probe@rig"
mkdir -p "$PROBE" && cp "$HERE/probe/extension.js" "$HERE/probe/metadata.json" "$PROBE/"

# ---- control: the sandboxed systemctl must NOT reach the real manager ----
if env -i PATH="$PATH" HOME="$RUN/home" XDG_RUNTIME_DIR="$RUN/runtime" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=$RUN/runtime/no-such-bus" \
      systemctl --user is-active gnome-speaks.service >/dev/null 2>&1; then
    setup_fail "sandboxed systemctl --user REACHED a user manager; refusing to run"
fi

# The bus socket is $XDG_RUNTIME_DIR/bus (unix:runtime=yes). sun_path is 108
# bytes; `unix:dir=` appends /dbus-XXXXXXXXXX and overflowed it from a
# worktree path (measured: "Failed to start message bus: Socket name too
# long", surfacing only as dbus-run-session's "EOF reading address"). Guard
# the shorter form too, so a deep checkout fails with a reason, not a riddle.
BUS_SOCK="$RUN/runtime/bus"
[ ${#BUS_SOCK} -le 100 ] || setup_fail "run dir too deep for a unix socket (${#BUS_SOCK} > 100 bytes): $BUS_SOCK"

# A private bus with NO service activation: the stock session.conf would let
# the shell pull in xdg-desktop-portal (which FUSE-mounts $XDG_RUNTIME_DIR/doc
# INSIDE the run dir and defeats cleanup), gnome-keyring, gvfs and a second
# ibus-daemon. Everything this rig needs is either the shell itself or a
# process the driver starts by hand.
BUSCONF="$RUN/bus.conf"
cat >"$BUSCONF" <<XML
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <keep_umask/>
  <listen>unix:runtime=yes</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
XML

export RUN SRC UUID
# The whole timeline runs inside one private bus. Nothing below is exported
# into THIS shell.
env -u DISPLAY -u WAYLAND_DISPLAY -u DBUS_SESSION_BUS_ADDRESS \
    HOME="$RUN/home" XDG_DATA_HOME="$RUN/data" XDG_CONFIG_HOME="$RUN/config" \
    XDG_CACHE_HOME="$RUN/cache" XDG_RUNTIME_DIR="$RUN/runtime" \
    GSETTINGS_BACKEND=keyfile XDG_SESSION_TYPE=wayland \
    dbus-run-session --config-file="$BUSCONF" -- bash "$HERE/driver.sh"
rc=$?
case $rc in
    0) ;;
    1) exit 1 ;;
    3) rig_fail "driver exit 3 (see $RUN/shell.log excerpt above)" ;;
    *) rig_fail "driver exit $rc" ;;
esac
