#!/usr/bin/env python3
"""repro: an IBus backend that was unreachable at start is retried later (#100).

At login the service can start before ibus-daemon. _init_typing() then calls
get_injector() once, the IBus attempt fails, and the ydotool fallback was
cached for the whole session -- injection_method=ibus silently meant ydotool
until a restart. The fix retries the build (bounded: once per
_INJECTOR_RETRY_SECONDS) while the cached backend is an unwanted fallback.

Verdicts (each printed, all must hold):
  R1  ibus unreachable at first use          -> backend is ydotool (fallback)
  R2  second call inside the interval        -> NO rebuild attempt (bounded)
  R3  daemon appears, interval elapsed       -> backend becomes ibus
  R4  once the wanted backend is cached, no further rebuilds happen
  R5  injection_method=ydotool never retries (nothing is "unwanted")

Run:  GS_SVC_PATH=<gnome-speaks-service.py> python3 repro_fallback_retry.py
Exit 0 = all hold; 1 = a verdict failed; 2 = setup failure (never a verdict).
"""
import atexit
import importlib.util
import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SVC_PATH = os.environ.get("GS_SVC_PATH", os.path.join(REPO_ROOT, "gnome-speaks-service.py"))
SCRATCH_ROOT = os.path.join(REPO_ROOT, "tmp", "repros")
_STATE = os.path.join(SCRATCH_ROOT, f"injector-seam-retry-{os.getpid()}", "state")
os.makedirs(_STATE, exist_ok=True)
atexit.register(shutil.rmtree, os.path.dirname(_STATE), True)
os.environ["XDG_STATE_HOME"] = _STATE
os.environ.setdefault("SPEECH_ENGINE_PATH", os.path.expanduser("~/Projects/speech-to-cli"))

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} ({label}) {detail}")
    if not cond:
        FAILS.append(label)


def load():
    sys.path.insert(0, os.path.dirname(SVC_PATH))
    spec = importlib.util.spec_from_file_location("gsvc_retry", SVC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gsvc_retry"] = mod
    spec.loader.exec_module(mod)
    mod.CONFIG.update({"key": "test-key", "wake_word": False, "chronicle": False,
                       "continuous_dictation": False, "conversation_mode": False,
                       "terminal_mode": False, "spiel_provider": False, "debug": False,
                       "wyoming_host": ""})
    mod.CONFIG_PATH = os.path.join(_STATE, "config.json")
    return mod


class FakeIbus:
    """Stands in for IbusInjector: reachability is a switch the test flips."""
    reachable = False
    builds = 0
    name = "ibus"

    def __init__(self, fallback=None):
        FakeIbus.builds += 1
        self.fallback = fallback

    def available(self):
        return FakeIbus.reachable

    def cancel(self):
        pass

    def prepare(self):
        pass


def main():
    if not os.path.isfile(SVC_PATH):
        print(f"!! SETUP FAILURE: no such file: {SVC_PATH}")
        return 2
    mod = load()
    if getattr(mod, "IbusInjector", None) is None and not hasattr(mod, "_make_injector"):
        print("!! SETUP FAILURE: service has no injector seam")
        return 2
    mod.IbusInjector = FakeIbus
    # A controllable clock: the fix (if present) keys its interval off time.monotonic.
    clock = {"t": 1000.0}
    real_monotonic = mod.time.monotonic
    mod.time.monotonic = lambda: clock["t"]
    try:
        mod._injector = None
        mod._injector_method = None
        mod.CONFIG["injection_method"] = "ibus"

        FakeIbus.reachable = False
        inj = mod.get_injector()
        check("R1", getattr(inj, "name", "") == "ydotool",
              f"first use with ibus unreachable -> {getattr(inj, 'name', '?')}")
        b1 = FakeIbus.builds

        clock["t"] += 1.0
        mod.get_injector()
        check("R2", FakeIbus.builds == b1,
              f"1 s later: builds {b1} -> {FakeIbus.builds} (must not rebuild inside the interval)")

        FakeIbus.reachable = True
        interval = float(getattr(mod, "_INJECTOR_RETRY_SECONDS", 15.0))
        clock["t"] += interval + 1.0
        inj = mod.get_injector()
        check("R3", getattr(inj, "name", "") == "ibus",
              f"daemon up, {interval + 1:.0f} s later -> {getattr(inj, 'name', '?')}")
        b3 = FakeIbus.builds

        clock["t"] += 10 * interval
        mod.get_injector()
        check("R4", FakeIbus.builds == b3,
              f"wanted backend cached: builds {b3} -> {FakeIbus.builds} (must not rebuild)")

        mod.CONFIG["injection_method"] = "ydotool"
        inj = mod.get_injector()
        b5 = FakeIbus.builds
        clock["t"] += 10 * interval
        mod.get_injector()
        check("R5", getattr(inj, "name", "") == "ydotool" and FakeIbus.builds == b5,
              f"explicit ydotool never retries: builds {b5} -> {FakeIbus.builds}")
    finally:
        mod.time.monotonic = real_monotonic

    if FAILS:
        print(f"FAIL: {len(FAILS)} verdict(s) -- an unreachable IBus at start is cached as ydotool for the whole session")
        return 1
    print("PASS: an unwanted ydotool fallback is retried once per interval and replaced when IBus appears")
    return 0


if __name__ == "__main__":
    sys.exit(main())
