#!/usr/bin/env python3
"""Startup deprecation warnings (#114) -- and the leak behind one of them.

Every service start logged three deprecation lines: `GLib.unix_signal_add is
deprecated; use GLibUnix.signal_add` twice (SIGTERM, SIGINT) and
`Gio.DBusConnection.register_object is deprecated` once -- PyGObject warns once
per deprecated GI function per PROCESS (measured: three registrations, one
line), so the second register site never showed.  The noise hid real warnings in the first
ten journal lines -- and the D-Bus one was not cosmetic.  Python's
`register_object` shadows `g_dbus_connection_register_object_with_closures`,
which GLib 2.84 deprecated for `_with_closures2` because the old closure was
handed an OWNED GDBusMethodInvocation that nothing released.  MEASURED on
PyGObject 3.56.2 / GLib 2.88: ~1.5 kB leaked per method call through
`register_object` (3012/2916/2796 kB per 2000 calls), flat through
`register_object_with_closures2`; refcount at handler entry 4 vs 3.  Every
GetState and every hotkey grew the service.

This runs the REAL `main()` -- not the two lines in isolation -- because the
fix is a pair of shims and a shim that is defined but not wired is exactly the
regression a call-site check cannot see:

  1. re-exec under a private `dbus-run-session`, so owning `org.gnome.Speaks`
     touches nothing on the desktop and needs no live service to be absent;
  2. import the service through the service-audit harness (isolated CONFIG,
     per-PID scratch, no mic / WS / injector), pin `spiel_provider` on so BOTH
     register sites run, HTTP on an ephemeral port;
  3. a verifier thread waits for the bus names, calls `GetState` and reads the
     Spiel `Name` property over the bus -- the registered closures must
     dispatch and return through whichever API was picked -- then sends
     SIGTERM to this process;
  4. `main()` must RETURN: only the signal source installed by the code under
     test can turn that SIGTERM into `loop.quit()`.  If the shim is broken the
     default disposition kills the process (rc 143) or the watchdog exits 1.

Warnings are recorded with `warnings.catch_warnings(record=True)` under
`simplefilter("always")` and counted only when they are attributed to the
service file itself, so PyGObject's own import-time `unix_signal_add_full`
chatter (fires under -W always, never in the journal) cannot pad the number.

CONTROL: before `main()` the instrument is proven live -- touching the
deprecated `GLib.unix_signal_add` attribute (or, if a future PyGObject has
removed it, registering a throwaway object with `register_object`) must record
a deprecation.  If neither deprecated API exists any more the check is
vacuous and exits 2, not 0.

Baseline: a20afea (main before #114) -> 3 deprecation lines, exit 1.
Fixed   : 0 lines, GetState answered, Spiel Name == "GNOME Speaks", exit 0.

exit 0 = clean, 1 = the defect is present, 2 = setup failure.
"""
import os
import shutil
import sys

# --- 1. private bus, BEFORE any harness import (the harness creates scratch at
# import and would leak it across an exec) --------------------------------------
if os.environ.get("GS_REPRO_PRIVATE_BUS") != "1":
    runner = shutil.which("dbus-run-session")
    if not runner:
        print("!! SETUP FAILURE: dbus-run-session not installed")
        sys.exit(2)
    env = dict(os.environ, GS_REPRO_PRIVATE_BUS="1")
    os.execve(runner, [runner, "--", sys.executable, os.path.abspath(__file__)] + sys.argv[1:], env)

import json          # noqa: E402
import signal        # noqa: E402
import threading     # noqa: E402
import time          # noqa: E402
import warnings      # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "service-audit"))
import harness  # noqa: E402

# main() runs restore_prior_engine() and the ydotool/IBus injector prepare()
# first thing; neither is under test and both reach for the real desktop.
os.environ["XDG_RUNTIME_DIR"] = os.path.join(harness.SCRATCH, "runtime")
os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)
os.environ["GNOME_SPEAKS_HTTP_PORT"] = "0"     # ephemeral, never 7710

import gi  # noqa: E402
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

SVC = os.path.realpath(harness.SVC_PATH)
SPIEL_IFACE = "org.freedesktop.Speech.Provider"


def fail(msg):
    print(f"[FAIL] {msg}")
    sys.exit(1)


def setup_failure(msg):
    print(f"!! SETUP FAILURE: {msg}")
    sys.exit(2)


# --- 2. the service, isolated ----------------------------------------------------
mod, _events = harness.load()


class _NullInjector:
    name = "null"

    def prepare(self): pass
    def recover(self): pass
    def end(self): pass
    def cancel(self): pass


def _no_network():
    raise RuntimeError("no network from a repro")


mod.restore_prior_engine = lambda reason="startup": False
mod.get_injector = lambda: _NullInjector()
mod.HAS_WS = False
mod.state.get_http_session = _no_network
for name in ("has_echo_cancel",):
    if hasattr(mod, name):
        setattr(mod, name, lambda *a, **k: False)
for name in ("_invalidate_stt_ws", "_discard_prewarmed_rec"):
    if hasattr(mod, name):
        setattr(mod, name, lambda *a, **k: None)

# Both register sites: pin the Spiel provider on, in CONFIG and in the scratch
# config file so a mid-run _reload_config_flags() cannot undo it.
mod.CONFIG["spiel_provider"] = True
with open(mod.CONFIG_PATH, encoding="utf-8") as f:
    _cfg = json.load(f)
_cfg["spiel_provider"] = True
with open(mod.CONFIG_PATH, "w", encoding="utf-8") as f:
    json.dump(_cfg, f)
harness.assert_isolated(mod)


def _deprecations(records):
    return [w for w in records
            if issubclass(w.category, DeprecationWarning)
            and os.path.realpath(w.filename) == SVC]


# --- CONTROL: the instrument must see a deprecation before we trust its zero ---
with warnings.catch_warnings(record=True) as ctl:
    warnings.simplefilter("always")
    control_api = None
    if hasattr(GLib, "unix_signal_add"):
        getattr(GLib, "unix_signal_add")          # attribute ACCESS is what warns
        control_api = "GLib.unix_signal_add"
    elif hasattr(Gio.DBusConnection, "register_object"):
        _c = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        _iface = Gio.DBusNodeInfo.new_for_xml(
            '<node><interface name="repro.Control"><method name="M"/></interface></node>'
        ).lookup_interface("repro.Control")
        _c.register_object("/repro/control", _iface, lambda *a: None, None, None)
        control_api = "Gio.DBusConnection.register_object"
n_ctl = sum(1 for w in ctl if issubclass(w.category, DeprecationWarning))
if control_api is None:
    setup_failure("neither deprecated API exists on this PyGObject; nothing to discriminate")
if n_ctl == 0:
    setup_failure(f"control {control_api} recorded 0 deprecations -- instrument is blind")
print(f"control: {control_api} -> {n_ctl} deprecation(s) recorded (instrument live)")

# --- 3. verifier: over the bus, then SIGTERM ---------------------------------------
result = {"GetState": None, "SpielName": None, "error": None, "main_returned": False}
main_done = threading.Event()


def _name_has_owner(conn, name):
    r = conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                       "org.freedesktop.DBus", "NameHasOwner",
                       GLib.Variant("(s)", (name,)), None, 0, 2000, None)
    return r.unpack()[0]


def verifier():
    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if _name_has_owner(conn, mod.BUS_NAME) and _name_has_owner(conn, mod.SPIEL_BUS_NAME):
                break
            time.sleep(0.05)
        else:
            result["error"] = "bus names never appeared"
            os.kill(os.getpid(), signal.SIGTERM)
            return
        r = conn.call_sync(mod.BUS_NAME, mod.OBJECT_PATH, mod.INTERFACE_NAME,
                           "GetState", None, None, 0, 5000, None)
        result["GetState"] = r.unpack()[0]
        r = conn.call_sync(mod.SPIEL_BUS_NAME, mod.SPIEL_OBJECT_PATH,
                           "org.freedesktop.DBus.Properties", "Get",
                           GLib.Variant("(ss)", (SPIEL_IFACE, "Name")), None, 0, 5000, None)
        result["SpielName"] = r.unpack()[0]
    except Exception as exc:  # noqa: BLE001
        result["error"] = repr(exc)
    finally:
        # 4. Only the source installed by the code under test can turn this into
        # loop.quit(). Watchdog: if main() has not returned, the handler is gone.
        os.kill(os.getpid(), signal.SIGTERM)
        if not main_done.wait(10.0):
            print("[FAIL] SIGTERM did not bring main() back within 10 s -- the signal "
                  "source under test is not installed")
            sys.stdout.flush()
            os._exit(1)


threading.Thread(target=verifier, name="verifier", daemon=True).start()

sys.argv = [SVC]
with warnings.catch_warnings(record=True) as rec:
    warnings.simplefilter("always")
    mod.main()
result["main_returned"] = True
main_done.set()

deps = _deprecations(rec)
print(f"deprecation lines attributed to the service during main(): {len(deps)}")
for w in deps:
    print(f"    {os.path.basename(w.filename)}:{w.lineno}: {w.category.__name__}: {w.message}")
print(f"GetState -> {result['GetState']!r}; Spiel Name -> {result['SpielName']!r}; "
      f"error={result['error']}")

if result["error"]:
    fail(f"bus round-trip failed: {result['error']}")
if not isinstance(result["GetState"], str):
    fail(f"GetState did not answer a string: {result['GetState']!r}")
if result["SpielName"] != "GNOME Speaks":
    fail(f"Spiel Name property wrong: {result['SpielName']!r}")
if deps:
    fail(f"{len(deps)} deprecation line(s) at service start (expected 0)")
print("[PASS] service start: 0 deprecation lines; both D-Bus objects answer; "
      "SIGTERM handled through the installed signal source")
