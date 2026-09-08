#!/usr/bin/env python3
"""repro: IBus focus on the daemon's FAKE context means "no text field" -- the
utterance must type through the fallback backend, not vanish (#109).

2026-09-07 11:08: a badge tap gave shell chrome stage key focus, ibus-daemon
focused its fake pseudo-client, and the recognised transcript was committed
into it and lost. Nothing fell back, nothing was typed, no toast.

  F1  focus in client="fake": commit() routes the text to the fallback,
      the engine receives NO commit, the IME is handed back (cancel)
  F2  the decision holds for the whole utterance: later partial/commit calls
      do not re-acquire (no second SetGlobalEngine) and still go to fallback
  F3  end() clears it: the next utterance with a real client commits via IBus
  F4  a real client (e.g. "gnome-terminal-server") commits via IBus, fallback
      untouched
  F5  a PASSWORD field: neither backend gets the text (secure beats fallback)

Run:  GS_SVC_PATH=<gnome-speaks-service.py> python3 repro_fake_context_fallback.py
Exit 0 = all hold; 1 = a verdict failed; 2 = setup failure.
"""
import atexit
import importlib
import os
import shutil
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SVC_PATH = os.environ.get("GS_SVC_PATH", os.path.join(REPO_ROOT, "gnome-speaks-service.py"))
WT = os.environ.get("GS_WT") or os.path.dirname(os.path.abspath(SVC_PATH))
SCRATCH_ROOT = os.path.join(REPO_ROOT, "tmp", "repros")
RUNTIME = os.path.join(SCRATCH_ROOT, f"ibus-fake-context-{os.getpid()}", "runtime")
os.makedirs(RUNTIME, exist_ok=True)
os.environ["XDG_RUNTIME_DIR"] = RUNTIME          # never the real breadcrumb
atexit.register(shutil.rmtree, os.path.dirname(RUNTIME), True)
sys.path.insert(0, WT)
FAILS = []


def check(label, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} ({label}) {detail}")
    if not cond:
        FAILS.append(label)


class FakeEngineDesc:
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name




class FakeConnection:
    """The daemon's GlobalEngine property, as GDBus hands it to a caller (#177).

    Unset -> a GLib.Error carrying the daemon's exact text; set -> `(v)` around
    a serialized IBusEngineDesc (child 2 is the name). No real daemon.
    """

    def __init__(self, bus):
        self._bus = bus

    def call_sync(self, dest, path, iface, member, params, reply_type, *rest):
        from gi.repository import GLib
        self._bus.n_property_get = getattr(self._bus, "n_property_get", 0) + 1
        assert (iface, member) == ("org.freedesktop.DBus.Properties", "Get"), (iface, member)
        assert params.unpack() == ("org.freedesktop.IBus", "GlobalEngine"), params.unpack()
        current = self._bus.current
        if not current:
            raise GLib.Error("GDBus.Error:org.freedesktop.DBus.Error.Failed: "
                             "No global engine.", "g-dbus-error-quark", 0)
        desc = GLib.Variant("(sa{sv}ssssssssussssssss)",
                            ("IBusEngineDesc", {}, current) + ("",) * 7 + (0,) + ("",) * 8)
        return GLib.Variant("(v)", (desc,))


class FakeBus:
    def __init__(self, prior="xkb:us::eng"):
        self.current = prior
        self.swaps = []

    def is_connected(self):
        return True

    def get_global_engine(self):
        return FakeEngineDesc(self.current) if self.current else None

    def set_global_engine(self, name):
        self.swaps.append(name)
        self.current = name
        return True

    def get_connection(self):
        return FakeConnection(self)      # #177: the property, asked directly


class FakeEngine:
    def __init__(self, client, purpose=0):
        self.purpose = purpose
        self.hints = 0
        self.focused = True
        self.client = client
        self.saw_content_type = purpose != 0
        self.commits = []
        self.preedits = []

    def is_secure(self):
        return self.purpose in ib._SECURE_PURPOSES

    def set_preedit(self, text):
        self.preedits.append(text)

    def clear_preedit(self):
        pass

    def commit(self, text):
        self.commits.append(text)
        return True


class FakeFallback:
    name = "ydotool"

    def __init__(self):
        self.commits = []
        self.enters = 0

    def prepare(self):
        pass

    def recover(self):
        pass

    def commit(self, text):
        self.commits.append(text)
        return True

    def press_enter(self):
        self.enters += 1
        return True


def wired(client, purpose=0):
    inj = ib.IbusInjector(fallback=FakeFallback())
    inj._bus = FakeBus()
    inj._engine = FakeEngine(client, purpose)
    inj._registered = True
    return inj


def flush(inj):
    # the coalescer commits on a timer; force it the way a session end does
    with inj._lock:
        inj._flush(allowed=True)


def main():
    global ib
    try:
        ib = importlib.import_module("ibus_injector")
    except Exception as exc:
        print(f"!! SETUP FAILURE: cannot import ibus_injector from {WT}: {exc}")
        return 2
    ib.FOCUS_WAIT = 0.01
    ib.CONTENT_TYPE_GRACE = 0.0
    prior_crumb = os.path.join(RUNTIME, "gnome-speaks", "prior-engine")

    # F1 — fake context
    inj = wired("fake")
    ok = inj.commit("hello from the badge tap")
    flush(inj)
    fb = inj._fallback.commits
    check("F1", ok and fb == ["hello from the badge tap"] and inj._engine.commits == []
          and inj._bus.current == "xkb:us::eng" and not inj._active,
          f"fallback={fb} engine={inj._engine.commits} global={inj._bus.current!r} active={inj._active}")

    # F2 — sticky for the utterance
    swaps_before = len(inj._bus.swaps)
    inj.set_preedit("hello from")
    inj.commit("second part")
    flush(inj)
    check("F2", len(inj._bus.swaps) == swaps_before and inj._fallback.commits[-1] == "second part"
          and inj._engine.commits == [],
          f"swaps {swaps_before}->{len(inj._bus.swaps)} fallback={inj._fallback.commits} engine={inj._engine.commits}")

    # F3 — end() clears; next utterance with a real client uses IBus
    inj.end()
    inj._engine = FakeEngine("gnome-terminal-server")
    inj.commit("real target now")
    flush(inj)
    check("F3", inj._engine.commits == ["real target now"] and inj._fallback.commits == ["hello from the badge tap", "second part"],
          f"engine={inj._engine.commits} fallback={inj._fallback.commits}")
    inj.end()

    # F4 — a real client from the start
    inj = wired("gnome-terminal-server")
    inj.commit("plain dictation")
    flush(inj)
    check("F4", inj._engine.commits == ["plain dictation"] and inj._fallback.commits == [],
          f"engine={inj._engine.commits} fallback={inj._fallback.commits}")
    inj.end()

    # F5 — password field: nothing anywhere
    inj = wired("gnome-terminal-server", purpose=8)   # IBUS_INPUT_PURPOSE_PASSWORD
    ok = inj.commit("hunter2")
    flush(inj)
    check("F5", not ok and inj._engine.commits == [] and inj._fallback.commits == [],
          f"ok={ok} engine={inj._engine.commits} fallback={inj._fallback.commits}")
    inj.end()

    if FAILS:
        print(f"FAIL: {len(FAILS)} verdict(s) -- text committed into IBus's fake context is lost")
        return 1
    print("PASS: a fake-context focus types via the fallback; real targets commit via IBus; passwords get nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
