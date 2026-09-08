#!/usr/bin/env python3
"""Repro for #177: every service start/stop logged libibus's "No global engine".

    ibus_bus_call_sync: org.freedesktop.DBus.Properties.Get:
        GDBus.Error:org.freedesktop.DBus.Error.Failed: No global engine.

Four times per restart (measured 2026-09-08: 106 lines in 24 h), from the
prior-engine snapshot/restore path reading the global engine through
`IBus.Bus.get_global_engine()`, which g_warning()s on every D-Bus error before
returning NULL. On GNOME an unset global engine is the STEADY STATE when the
only input source is an xkb layout -- `ibus engine` says "No engine is set."
while the keyboard works -- so the line read as the crash state this module
guards against, on a healthy desktop, at every restart.

The fix reads the GlobalEngine property over the bus's own GDBusConnection
(`global_engine_name()`), where "unset" is a GLib.Error nobody logs, and makes
"no breadcrumb, nothing set" an explicit no-op with ONE INFO line.

Cases (stderr captured at the FD level -- the warning is C-side, not Python):
  N0  positive control: the fake's libibus-shaped get_global_engine() DOES
      write the warning into the capture (else a zero below proves nothing)
  N1  restore_prior_engine(), no breadcrumb, nothing set: 0 warnings,
      no swap, returns False, exactly one INFO "nothing to restore"
  N2  the same call again ("service stop"): still one INFO per call
  N3  acquire()/end(): 0 warnings; swaps unchanged (ours, then the derived
      restore target); breadcrumb consumed -- injection behaviour unchanged
  N4  THE DISCRIMINATOR: breadcrumb present + nothing set = the crash state.
      Restored (swap to the recorded engine, returns True, breadcrumb
      consumed), and still 0 warnings
  N5  guard, both sides green: an engine IS set (the property answers a
      serialized EngineDesc); breadcrumb naming a different engine -> restore
      swaps to it; breadcrumb naming the current one -> no swap, stale
      breadcrumb cleared. Exercises the variant decode on the fixed tree

No real daemon: `ib.IBus` is replaced by a fake module whose Bus() is a fake
bus; no SetGlobalEngine reaches the desktop. XDG_RUNTIME_DIR is redirected
so the real breadcrumb is never read or written. Exit 0 = fixed, 1 = the
defect is present, 2 = the check could not run.

Baseline: RED on a715022 (N1, N2, N3, N4 -- one warning each and no INFO).
"""

import atexit
import logging
import os
import shutil
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SVC_DEFAULT = os.path.join(REPO_ROOT, "gnome-speaks-service.py")
SCRATCH_ROOT = os.path.join(REPO_ROOT, "tmp", "repros")

SVC_PATH = os.environ.get("GS_SVC_PATH", SVC_DEFAULT)
WT = os.environ.get("GS_WT") or os.path.dirname(os.path.abspath(SVC_PATH))
SCRATCH = os.path.join(SCRATCH_ROOT, f"no-global-engine-{os.getpid()}")
RUNTIME = os.path.join(SCRATCH, "runtime")
shutil.rmtree(SCRATCH, ignore_errors=True)
os.makedirs(RUNTIME, exist_ok=True)
os.environ["XDG_RUNTIME_DIR"] = RUNTIME          # never the real breadcrumb
atexit.register(shutil.rmtree, SCRATCH, True)
sys.path.insert(0, WT)

try:
    import ibus_injector as ib  # noqa: E402
    from ibus_injector import IbusInjector  # noqa: E402
    from gi.repository import GLib  # noqa: E402
except Exception as exc:  # pragma: no cover - setup, not the defect
    print(f"!! SETUP FAILURE: cannot import ibus_injector from {WT}: {exc}")
    sys.exit(2)

if not ib.prior_engine_path().startswith(RUNTIME):
    print(f"!! SETUP FAILURE: breadcrumb path escaped the sandbox: "
          f"{ib.prior_engine_path()}")
    sys.exit(2)

WARNING_LINE = ("ibus_bus_call_sync: org.freedesktop.DBus.Properties.Get: "
                "GDBus.Error:org.freedesktop.DBus.Error.Failed: No global engine.")

FAILS = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}  <- {detail}")
        FAILS.append(label)


# ---- fakes -----------------------------------------------------------------

class FakeEngineDesc:
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


class FakeConnection:
    """The daemon's GlobalEngine property, as GDBus hands it to a caller.

    Unset -> a GLib.Error carrying the daemon's exact text (bus/ibusimpl.c);
    set -> `(v)` around a serialized IBusEngineDesc, child 2 the name.
    """

    def __init__(self, bus):
        self._bus = bus

    def call_sync(self, dest, path, iface, member, params, reply_type, *rest):
        self._bus.n_property_get += 1
        assert (iface, member) == ("org.freedesktop.DBus.Properties", "Get"), (iface, member)
        assert params.unpack() == ("org.freedesktop.IBus", "GlobalEngine"), params.unpack()
        if not self._bus.current:
            raise GLib.Error("GDBus.Error:org.freedesktop.DBus.Error.Failed: "
                             "No global engine.", "g-dbus-error-quark", 0)
        desc = GLib.Variant("(sa{sv}ssssssssussssssss)",
                            ("IBusEngineDesc", {}, self._bus.current)
                            + ("",) * 7 + (0,) + ("",) * 8)
        return GLib.Variant("(v)", (desc,))


class FakeBus:
    """An IBus.Bus with libibus's stderr habit reproduced faithfully.

    get_global_engine() on an unset engine does what `ibus_bus_call_sync`
    does (measured, libibus 1.5.34, `ibus engine` and from Python): writes
    the IBUS-WARNING to fd 2 and returns NULL. It is written with os.write so
    the capture below sees exactly what a journal would.
    """

    def __init__(self, current=None, engines=("xkb:us::eng", "xkb:de::ger")):
        self.current = current
        self.engines = list(engines)
        self.swaps = []
        self.n_get_global = 0
        self.n_property_get = 0

    def is_connected(self):
        return True

    def get_connection(self):
        return FakeConnection(self)

    def get_global_engine(self):
        self.n_get_global += 1
        if not self.current:
            os.write(2, f"\n(process:{os.getpid()}): IBUS-WARNING **: "
                        f"{WARNING_LINE}\n".encode())
            return None
        return FakeEngineDesc(self.current)

    def set_global_engine(self, name):
        self.swaps.append(name)
        self.current = name
        return True

    def list_engines(self):
        return [FakeEngineDesc(n) for n in self.engines]


class FakeIBusModule:
    def __init__(self, bus):
        self._bus = bus

    def init(self):
        pass

    def Bus(self):
        return self._bus


class FakeSettings:
    def __init__(self, sources):
        self._v = {"mru-sources": [], "sources": sources}

    def get_value(self, key):
        class V:
            def __init__(self, val):
                self._val = val

            def unpack(self):
                return self._val
        return V(self._v[key])


class GioShim:
    settings = None

    class Settings:
        @staticmethod
        def new(schema):
            return GioShim.settings

    class DBusCallFlags:
        NONE = 0


class FakeEngine:
    def __init__(self):
        self.purpose = 0
        self.focused = True
        self.client = "gedit"
        self.commits = []
        self.preedits = []
        self.cleared = 0

    def is_secure(self):
        return self.purpose in ib._SECURE_PURPOSES

    def set_preedit(self, text):
        pass

    def clear_preedit(self):
        self.cleared += 1

    def commit(self, text):
        self.commits.append(text)
        return True


class FakeFallback:
    name = "ydotool"

    def prepare(self):
        pass

    def commit(self, text):
        return True

    def press_enter(self):
        return True

    def recover(self):
        pass

    def end(self):
        pass

    def cancel(self):
        pass


def wired(bus):
    inj = IbusInjector(fallback=FakeFallback())
    inj._bus = bus
    inj._engine = FakeEngine()
    inj._registered = True
    return inj


# ---- instruments -----------------------------------------------------------

class StderrCapture:
    """Redirect fd 2 into a file for the duration. C-side writes included."""

    def __init__(self):
        self.path = os.path.join(SCRATCH, "stderr-%d.txt" % id(self))

    def __enter__(self):
        sys.stderr.flush()
        self._saved = os.dup(2)
        self._fh = open(self.path, "wb")
        os.dup2(self._fh.fileno(), 2)
        return self

    def __exit__(self, *exc):
        sys.stderr.flush()
        os.dup2(self._saved, 2)
        os.close(self._saved)
        self._fh.close()
        return False

    def warnings(self):
        with open(self.path, encoding="utf-8", errors="replace") as fh:
            return sum(1 for line in fh if "No global engine" in line)


class LogCapture:
    def __init__(self):
        self.records = []

    def __enter__(self):
        class H(logging.Handler):
            def emit(_self, record):
                self.records.append(record)
        self._h = H(level=logging.DEBUG)
        ib.log.addHandler(self._h)
        self._level = ib.log.level
        ib.log.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        ib.log.removeHandler(self._h)
        ib.log.setLevel(self._level)
        return False

    def infos(self, needle):
        return [r for r in self.records
                if r.levelno == logging.INFO and needle in r.getMessage()]


def main():
    print(f"XDG_RUNTIME_DIR redirected to {RUNTIME}")
    print(f"ibus_injector from {WT}\n")

    real_ibus, real_has, real_gio = ib.IBus, ib.HAS_IBUS, getattr(ib, "Gio", None)
    ib.HAS_IBUS = True
    ib.Gio = GioShim
    GioShim.settings = FakeSettings([("xkb", "us")])
    try:
        # -- [N0] the instrument sees a warning when one is written ---------
        print("[N0] positive control: the capture sees libibus's line")
        bus = FakeBus(current=None)
        with StderrCapture() as cap:
            bus.get_global_engine()
        n = cap.warnings()
        if n != 1:
            print(f"!! SETUP FAILURE: control wrote 1 warning, capture saw {n}")
            return 2
        print(f"  measured: 1 libibus-shaped warning -> capture counted {n}")

        # -- [N1] service start: no breadcrumb, nothing set -----------------
        print("\n[N1] restore_prior_engine('service start'): no breadcrumb, no engine set")
        bus = FakeBus(current=None)
        ib.IBus = FakeIBusModule(bus)
        ib.clear_prior_engine()
        with StderrCapture() as cap, LogCapture() as logs:
            did = ib.restore_prior_engine("service start")
        n = cap.warnings()
        infos = logs.infos("nothing to restore")
        print(f"  measured: warnings={n} swaps={bus.swaps} returned={did} "
              f"INFO-nothing-to-restore={len(infos)}")
        check("no libibus 'No global engine' warning on stderr", n == 0, f"{n} lines")
        check("restore is a no-op (no SetGlobalEngine)", bus.swaps == [], bus.swaps)
        check("returns False (nothing restored)", did is False, did)
        check("exactly one INFO line says nothing to restore", len(infos) == 1,
              [r.getMessage() for r in logs.records if r.levelno >= logging.INFO])

        # -- [N2] service stop: the same, once per call ----------------------
        print("\n[N2] restore_prior_engine('service stop'): same answer, one INFO per call")
        with StderrCapture() as cap, LogCapture() as logs:
            did = ib.restore_prior_engine("service stop")
        n = cap.warnings()
        infos = logs.infos("nothing to restore")
        check("no warning on the stop path either", n == 0, f"{n} lines")
        check("one INFO line for this call too", len(infos) == 1 and did is False,
              (len(infos), did))
        check("the INFO names the reason it ran for",
              infos and "service stop" in infos[0].getMessage(),
              infos[0].getMessage() if infos else "no INFO")

        # -- [N3] acquire()/end(): the per-utterance snapshot -----------------
        print("\n[N3] acquire()/end() with no global engine set")
        bus = FakeBus(current=None)
        inj = wired(bus)
        with StderrCapture() as cap:
            ok = inj.acquire()
            crumb = ib.read_prior_engine()
            inj.end()
        n = cap.warnings()
        print(f"  measured: warnings={n} acquire={ok} crumb={crumb!r} swaps={bus.swaps}")
        check("no warning during the snapshot", n == 0, f"{n} lines")
        check("acquire still succeeds", ok is True, ok)
        check("breadcrumb was the derived target (input-sources)",
              crumb == "xkb:us::eng", crumb)
        check("swap sequence unchanged: ours, then the derived restore target",
              bus.swaps == [ib.ENGINE_NAME, "xkb:us::eng"], bus.swaps)
        check("breadcrumb consumed after restore", ib.read_prior_engine() is None)

        # -- [N4] the discriminator: breadcrumb + nothing set = crash state ---
        print("\n[N4] crash state: breadcrumb present AND no global engine set")
        bus = FakeBus(current=None)
        ib.IBus = FakeIBusModule(bus)
        ib.write_prior_engine("xkb:us::eng")
        with StderrCapture() as cap, LogCapture() as logs:
            did = ib.restore_prior_engine("service start")
        n = cap.warnings()
        print(f"  measured: warnings={n} swaps={bus.swaps} returned={did}")
        check("still no libibus warning", n == 0, f"{n} lines")
        check("the recorded engine IS restored", bus.swaps == ["xkb:us::eng"], bus.swaps)
        check("returns True (a restore happened)", did is True, did)
        check("breadcrumb consumed", ib.read_prior_engine() is None)
        check("...and this case does NOT claim 'nothing to restore'",
              not logs.infos("nothing to restore"))

        # -- [N5] guards: an engine IS set ------------------------------------
        print("\n[N5] guards: a global engine is set (property answers an EngineDesc)")
        bus = FakeBus(current="xkb:de::ger")
        ib.IBus = FakeIBusModule(bus)
        ib.write_prior_engine("xkb:us::eng")
        with StderrCapture() as cap:
            did = ib.restore_prior_engine("test")
        check("breadcrumb names a different engine -> restored to it",
              did is True and bus.swaps == ["xkb:us::eng"], (did, bus.swaps))
        check("no warning (nothing was unset)", cap.warnings() == 0)

        bus = FakeBus(current="xkb:us::eng")
        ib.IBus = FakeIBusModule(bus)
        ib.write_prior_engine("xkb:us::eng")
        with StderrCapture() as cap, LogCapture() as logs:
            did = ib.restore_prior_engine("test")
        check("breadcrumb names the current engine -> no swap, stale breadcrumb cleared",
              did is False and bus.swaps == [] and ib.read_prior_engine() is None,
              (did, bus.swaps, ib.read_prior_engine()))
        check("a set engine with no breadcrumb is left alone, quietly",
              ib.restore_prior_engine("test") is False and bus.swaps == []
              and not logs.infos("nothing to restore"))
    finally:
        ib.IBus, ib.HAS_IBUS = real_ibus, real_has
        if real_gio is not None:
            ib.Gio = real_gio
        ib.clear_prior_engine()

    print()
    if FAILS:
        print(f"FAILURES: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
