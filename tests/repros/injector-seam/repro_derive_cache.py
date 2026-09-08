#!/usr/bin/env python3
"""Repro for #136: acquire() re-derived the IBus restore target every utterance.

On GNOME the shell owns input sources and leaves the daemon's global engine
unset, so `GetGlobalEngine` answers empty on EVERY acquire and the "fallback"
`derive_restore_target()` is the normal path. Two costs rode along on each
utterance, serial with the first keystroke:

  * `bus.list_engines()` -- a synchronous D-Bus call that deserialises every
    EngineDesc the daemon knows (974 on the desk it was measured on, 23 ms)
    to find one name that never changes while the daemon is up;
  * one `IBUS-WARNING ... No global engine` in the journal per utterance,
    because libibus g_warning()s on the failed GetGlobalEngine.

This script counts both against a fake bus across N acquire/end cycles, then
changes the simulated input-sources setting and checks the cache notices --
and that a miss is never remembered. Exit 0 = fixed, 1 = the defect is
present, 2 = the check could not run.

No real daemon, no SetGlobalEngine, XDG_RUNTIME_DIR redirected (the real
breadcrumb is never read or written).
"""

import atexit
import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SVC_DEFAULT = os.path.join(REPO_ROOT, "gnome-speaks-service.py")
SCRATCH_ROOT = os.path.join(REPO_ROOT, "tmp", "repros")

SVC_PATH = os.environ.get("GS_SVC_PATH", SVC_DEFAULT)
WT = os.environ.get("GS_WT") or os.path.dirname(os.path.abspath(SVC_PATH))
RUNTIME = os.path.join(SCRATCH_ROOT, f"derive-cache-{os.getpid()}", "runtime")
shutil.rmtree(RUNTIME, ignore_errors=True)
os.makedirs(RUNTIME, exist_ok=True)
os.environ["XDG_RUNTIME_DIR"] = RUNTIME          # never the real breadcrumb
atexit.register(shutil.rmtree, os.path.dirname(RUNTIME), True)
sys.path.insert(0, WT)

try:
    import ibus_injector as ib  # noqa: E402
    from ibus_injector import IbusInjector  # noqa: E402
except Exception as exc:  # pragma: no cover - setup, not the defect
    print(f"!! SETUP FAILURE: cannot import ibus_injector from {WT}: {exc}")
    sys.exit(2)

if not ib.prior_engine_path().startswith(RUNTIME):
    print(f"!! SETUP FAILURE: breadcrumb path escaped the sandbox: "
          f"{ib.prior_engine_path()}")
    sys.exit(2)

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


class CountingBus:
    """GNOME's shape: no global engine, ever. Counts what it is asked."""

    def __init__(self, engines):
        self.engines = list(engines)
        self.current = None
        self.swaps = []
        self.n_get_global = 0
        self.n_list = 0

    def is_connected(self):
        return True

    def get_global_engine(self):
        self.n_get_global += 1
        return None                      # libibus: NULL + an IBUS-WARNING

    def set_global_engine(self, name):
        self.swaps.append(name)
        self.current = name
        return True

    def list_engines(self):
        self.n_list += 1
        return [FakeEngineDesc(n) for n in self.engines]


class FakeSettings:
    def __init__(self, mru, sources):
        self._v = {"mru-sources": mru, "sources": sources}

    def get_value(self, key):
        class V:
            def __init__(self, val):
                self._val = val

            def unpack(self):
                return self._val
        return V(self._v[key])


class GioShim:
    settings = None
    n_new = 0

    class Settings:
        @staticmethod
        def new(schema):
            GioShim.n_new += 1
            return GioShim.settings


class FakeEngine:
    """Same shape as verify_ibus_injector.FakeEngine: a focused, ordinary field."""

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
        if text:
            self.preedits.append(text)
        else:
            self.clear_preedit()

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


ENGINES = ["xkb:us::eng", "xkb:us:dvorak:eng", "xkb:de:neo:deu", "xkb:de:neo:eng"]


def main():
    print(f"XDG_RUNTIME_DIR redirected to {RUNTIME}")
    print(f"ibus_injector from {WT}\n")

    real_gio = getattr(ib, "Gio", None)
    ib.Gio = GioShim
    try:
        # -- [1] N utterances, inputs unchanged ------------------------------
        print("[1] N acquire/end cycles with input-sources unchanged")
        N = 5
        GioShim.settings = FakeSettings([], [("xkb", "us")])
        bus = CountingBus(ENGINES)
        inj = wired(bus)
        crumbs = []
        for _ in range(N):
            ok = inj.acquire()
            crumbs.append(ib.read_prior_engine())
            inj.end()
            if not ok:
                break
        print(f"  measured: acquires={N} list_engines={bus.n_list} "
              f"get_global_engine={bus.n_get_global} Settings.new={GioShim.n_new}")
        check("every acquire still succeeded", ok is True)
        check("breadcrumb was the derived target on every cycle",
              crumbs == ["xkb:us::eng"] * N, crumbs)
        check("every cycle restored to the derived target",
              bus.swaps == [ib.ENGINE_NAME, "xkb:us::eng"] * N, bus.swaps)
        check("breadcrumb cleared after the last restore",
              ib.read_prior_engine() is None)
        check(f"list_engines() issued once for {N} utterances (was {N})",
              bus.n_list == 1, f"n_list={bus.n_list}")
        check(f"GetGlobalEngine asked once for {N} utterances (was {N}: "
              "one IBUS-WARNING per utterance)",
              bus.n_get_global == 1, f"n_get_global={bus.n_get_global}")

        # -- [2] the user switches layout -------------------------------------
        print("\n[2] input-sources changes between utterances")
        GioShim.settings = FakeSettings([("xkb", "us+dvorak")], [("xkb", "us")])
        inj.acquire()
        crumb = ib.read_prior_engine()
        inj.end()
        check("next utterance restores to the NEW layout",
              crumb == "xkb:us:dvorak:eng" and bus.swaps[-1] == "xkb:us:dvorak:eng",
              f"crumb={crumb} last_swap={bus.swaps[-1]}")
        check("list_engines() issued a second time for the new layout",
              bus.n_list == 2, f"n_list={bus.n_list}")
        check("the change did NOT re-trigger GetGlobalEngine (still empty on GNOME)",
              bus.n_get_global == 1, f"n_get_global={bus.n_get_global}")

        GioShim.settings = FakeSettings([], [("xkb", "us")])
        inj.acquire()
        crumb = ib.read_prior_engine()
        inj.end()
        check("switching back restores the ORIGINAL layout",
              crumb == "xkb:us::eng", crumb)
        check("...from memory, without a third list_engines()",
              bus.n_list == 2, f"n_list={bus.n_list}")

        # -- [3] never cache a miss --------------------------------------------
        print("\n[3] a miss is not remembered")
        GioShim.settings = FakeSettings([], [("xkb", "zz")])
        before = bus.n_list
        first = ib.derive_restore_target(bus)
        second = ib.derive_restore_target(bus)
        check("unknown layout still yields the unverified guess",
              first == second == "xkb:zz::eng", (first, second))
        check("...and the daemon is re-asked next time (no negative cache)",
              bus.n_list == before + 2, f"n_list={bus.n_list} before={before}")

        GioShim.settings = FakeSettings([], [])
        check("no configured sources -> None, uncached",
              ib.derive_restore_target(bus) is None)
        GioShim.settings = FakeSettings([], [("xkb", "us")])
        before = bus.n_list
        check("a valid setting right after a None derives from memory",
              ib.derive_restore_target(bus) == "xkb:us::eng" and bus.n_list == before,
              f"n_list={bus.n_list}")

        # -- [4] a different bus starts cold -----------------------------------
        print("\n[4] the cache is per bus (reconnect / startup probe)")
        probe = CountingBus(ENGINES)
        name = ib.derive_restore_target(probe)
        check("a fresh bus is asked itself, not served another bus's memory",
              name == "xkb:us::eng" and probe.n_list == 1,
              f"name={name} probe.n_list={probe.n_list}")
        probe_de = CountingBus(["xkb:de:neo:deu"])
        GioShim.settings = FakeSettings([], [("xkb", "de+neo")])
        check("...and gets ITS daemon's answer (de+neo -> :deu here)",
              ib.derive_restore_target(probe_de) == "xkb:de:neo:deu")

        # -- [5] restore_prior_engine() stays dependency-free ------------------
        print("\n[5] restore_prior_engine() still needs no config and no instance")
        src = open(os.path.join(WT, "ibus_injector.py"), encoding="utf-8").read()
        for tok in ("CONFIG", "load_config", "import state", "gnome_speaks_service"):
            check(f"ibus_injector.py does not reference {tok!r}",
                  tok not in src)
        check("restore_prior_engine() is callable with no arguments",
              callable(getattr(ib, "restore_prior_engine", None)))

        # -- [6] the live session was never touched ----------------------------
        print("\n[6] live session untouched")
        check("no real prior-engine file outside the redirected runtime dir",
              ib.prior_engine_path().startswith(RUNTIME), ib.prior_engine_path())
    finally:
        if real_gio is not None:
            ib.Gio = real_gio

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s) red -- #136 restore-target re-derivation")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("PASS: restore target derived once per bus/layout, "
          "input-sources changes still honoured, misses never cached")
    return 0


if __name__ == "__main__":
    # A harness crash is NOT "the defect is present": exit 3 so the runner
    # labels it HARNESS INCOMPLETE instead of counting a red it never measured.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:  # pragma: no cover
        import traceback
        traceback.print_exc()
        print("!! HARNESS DID NOT COMPLETE")
        sys.exit(3)
