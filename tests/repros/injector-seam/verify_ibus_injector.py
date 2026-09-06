#!/usr/bin/env python3
"""Verify the IBus injection backend against a MOCK bus.

Deliberately never touches the live session: no IBus.Bus() is constructed, no
SetGlobalEngine is issued, and XDG_RUNTIME_DIR is redirected so the real
prior-engine breadcrumb is never read or written. The only sanctioned live
touch in this project is the Phase 0 probe, and this is not it.

Covers the safety properties that make the backend shippable:
  * the prior engine is persisted BEFORE the swap, cleared only after restore
  * password/PIN fields are refused at acquire AND re-checked on every commit
  * restore is idempotent (restore-once) and survives being called from
    error paths
  * the watchdog force-restores a session that outlives its bound
  * a crash breadcrumb is replayed by restore_prior_engine()
  * press_enter never becomes a text commit
  * commit coalescing joins whitespace without doubling it
"""
import atexit
import importlib.util
import os
import shutil
import sys
import time

# ENV CONTRACT (unified 2026-09-06): GS_SVC_PATH is the ONE input -- the
# service.py file under test. The worktree dir is DERIVED from its dirname,
# so ibus_injector.py, which is what this file actually imports always come from
# the same tree as the service. GS_WT overrides the dir only if you really
# mean to mix trees. Defaults to the main checkout; the old defaults pointed
# at ~/Projects/gnome-speaks-wt/<name>/ worktrees that no longer exist, so a
# bare run died with FileNotFoundError instead of testing anything.
# This file used to read GS_WT ONLY and ignore GS_SVC_PATH, so a caller
# following the suite-wide contract silently tested the WRONG tree --
# PYTHONPATH appeared to work only by accident of a bogus sys.path entry.
# Repo-relative by construction: this file lives at
# tests/repros/<suite>/<file>.py, so four dirnames reach the repo root. #59:
# the old defaults were absolute paths into ~/Projects/gnome-speaks-wt/<name>/
# worktrees that no longer existed, so a bare run died with FileNotFoundError
# instead of testing anything.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SVC_DEFAULT = os.path.join(REPO_ROOT, "gnome-speaks-service.py")
# Scratch lives in the repo's gitignored tmp/, keyed by PID. Never a fixed
# shared path: two agents running a suite at once used to corrupt each other
# and it read exactly like a service regression.
SCRATCH_ROOT = os.path.join(REPO_ROOT, "tmp", "repros")

SVC_PATH = os.environ.get("GS_SVC_PATH", SVC_DEFAULT)
WT = os.environ.get("GS_WT") or os.path.dirname(os.path.abspath(SVC_PATH))
# Per-PID: this dir is rmtree()d at import, so a FIXED path meant two
# agents running this file at once destroyed each other's breadcrumb.
RUNTIME = os.path.join(SCRATCH_ROOT, f"morpheus-ibus-tests-{os.getpid()}", "runtime")
shutil.rmtree(RUNTIME, ignore_errors=True)
os.makedirs(RUNTIME, exist_ok=True)
os.environ["XDG_RUNTIME_DIR"] = RUNTIME          # never the real breadcrumb
atexit.register(shutil.rmtree, os.path.dirname(RUNTIME), True)
sys.path.insert(0, WT)

import ibus_injector as ib  # noqa: E402
from ibus_injector import IbusInjector, _Coalescer  # noqa: E402

FAILS = []


def capture_log():
    """Attach a buffer to the backend's logger; returns (buffer, detach)."""
    import io
    import logging
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setLevel(logging.DEBUG)
    ib.log.addHandler(h)
    return buf, lambda: ib.log.removeHandler(h)


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}  <- {detail}")
        FAILS.append(label)


class FakeEngineDesc:
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


class FakeBus:
    """Records engine swaps. Never speaks to a real daemon."""

    def __init__(self, prior="xkb:us::eng", accept=True, raise_on_get=False):
        self.current = prior
        self.accept = accept
        self.raise_on_get = raise_on_get
        self.swaps = []

    def is_connected(self):
        return True

    def get_global_engine(self):
        if self.raise_on_get:
            # What GNOME actually does: GetGlobalEngine fails outright
            # ("No global engine") because the shell owns input sources.
            raise RuntimeError("No global engine")
        return FakeEngineDesc(self.current) if self.current else None

    def set_global_engine(self, name):
        self.swaps.append(name)
        if not self.accept:
            return False
        self.current = name
        return True


class FakeEngine:
    """Stands in for the object the daemon would drive."""

    def __init__(self, purpose=0, preedit_capable=True):
        self.purpose = purpose
        self.focused = True
        self.commits = []
        self.preedits = []
        self.cleared = 0
        self._preedit_capable = preedit_capable

    def is_secure(self):
        return self.purpose in ib._SECURE_PURPOSES

    def set_preedit(self, text):
        if not text:
            self.clear_preedit()
            return
        if not self._preedit_capable:
            return
        self.preedits.append(text)

    def clear_preedit(self):
        self.cleared += 1

    def commit(self, text):
        self.commits.append(text)
        return True


class FakeFallback:
    name = "ydotool"

    def __init__(self):
        self.enters = 0
        self.recovers = 0
        self.prepared = 0

    def prepare(self):
        self.prepared += 1

    def press_enter(self):
        self.enters += 1
        return True

    def recover(self):
        self.recovers += 1


def wired(prior="xkb:us::eng", purpose=0, accept=True, preedit_capable=True,
          raise_on_get=False):
    """An IbusInjector pre-wired to fakes, bypassing real registration."""
    inj = IbusInjector(fallback=FakeFallback())
    inj._bus = FakeBus(prior, accept=accept, raise_on_get=raise_on_get)
    inj._engine = FakeEngine(purpose, preedit_capable=preedit_capable)
    inj._registered = True
    return inj


def flush_now(inj):
    """Force the coalescer out without waiting on its timer."""
    inj._cancel_timers()
    inj._flush(allowed=True)


def main():
    print(f"backend under test: {WT}/ibus_injector.py")
    print(f"XDG_RUNTIME_DIR redirected to {RUNTIME}\n")

    # ---- identity / the keymap bug we must not copy ----------------------
    print("[1] identity")
    check("engine layout is 'default', never 'us'", ib.ENGINE_LAYOUT == "default",
          ib.ENGINE_LAYOUT)
    check("component name is ours", ib.COMPONENT_NAME.endswith("GnomeSpeaks"))
    check("secure purposes are PASSWORD/PIN", ib._SECURE_PURPOSES == (8, 9))
    if ib.HAS_IBUS:
        eng_cls = ib.SpeaksEngine
        check("do_process_key_event returns False (keys pass through)",
              eng_cls.do_process_key_event(object(), 0, 0, 0) is False)
        check("engine implements both focus spellings",
              all(hasattr(eng_cls, m) for m in
                  ("do_focus_in", "do_focus_in_id", "do_focus_out", "do_focus_out_id")))
        check("engine implements do_set_content_type",
              hasattr(eng_cls, "do_set_content_type"))
    else:
        print("  --   IBus bindings absent; engine-class checks skipped")

    # ---- coalescing ------------------------------------------------------
    print("\n[2] commit coalescing")
    c = _Coalescer()
    c.push("This is")
    c.push("not working")
    check("stripped segments get a separator", c.pending == "This is not working", c.pending)
    c2 = _Coalescer()
    c2.push("hello")
    c2.push(" world")
    check("natural leading space is not doubled", c2.pending == "hello world", c2.pending)
    c3 = _Coalescer()
    c3.push("a")
    c3.committed_any = True
    check("later flush is spaced from earlier commit", c3.take() == " a", repr(c3.take()))
    c4 = _Coalescer()
    c4.push("secret")
    check("take(allowed=False) DISCARDS", c4.take(allowed=False) == "", "leaked")
    check("...and leaves nothing buffered", c4.pending == "")

    # ---- acquire ordering: persist BEFORE swap ---------------------------
    print("\n[3] acquire persists the way back BEFORE swapping")
    inj = wired()
    ok = inj.acquire()
    check("acquire() succeeded", ok is True)
    check("swapped to our engine", inj._bus.swaps == [ib.ENGINE_NAME], inj._bus.swaps)
    check("breadcrumb written", ib.read_prior_engine() == "xkb:us::eng",
          ib.read_prior_engine())
    check("watchdog armed", inj._watchdog is not None)
    inj.end()
    check("end() restored the prior engine",
          inj._bus.swaps == [ib.ENGINE_NAME, "xkb:us::eng"], inj._bus.swaps)
    check("breadcrumb cleared after clean restore", ib.read_prior_engine() is None)
    check("watchdog disarmed", inj._watchdog is None)

    # write-before-swap ordering, proven by refusing the swap
    inj = wired(accept=False)
    ok = inj.acquire()
    check("acquire() returns False when the swap is refused", ok is False)
    check("refused swap leaves no stale breadcrumb", ib.read_prior_engine() is None,
          ib.read_prior_engine())
    check("refused swap leaves us inactive", inj._active is False)

    # ---- secure fields ---------------------------------------------------
    print("\n[4] password/PIN refusal")
    for purpose, label in ((8, "PASSWORD"), (9, "PIN")):
        inj = wired(purpose=purpose)
        ok = inj.acquire()
        check(f"acquire() refuses a {label} field", ok is False)
        check(f"{label}: engine restored on refusal",
              inj._bus.swaps[-1] == "xkb:us::eng", inj._bus.swaps)
        check(f"{label}: breadcrumb cleared", ib.read_prior_engine() is None)
        check(f"{label}: nothing was committed", inj._engine.commits == [])

    # late content-type: ordinary field at acquire, secure by commit time
    inj = wired(purpose=0)
    inj.acquire()
    inj._engine.purpose = 8
    got = inj.commit("hunter2")
    flush_now(inj)
    check("mid-session focus into a password field refuses the commit", got is False)
    check("...and commits nothing", inj._engine.commits == [], inj._engine.commits)
    check("...and hands the input method back",
          inj._bus.swaps[-1] == "xkb:us::eng", inj._bus.swaps)

    # ---- restore-once / idempotency --------------------------------------
    print("\n[5] restore is idempotent")
    inj = wired()
    inj.acquire()
    inj.end()
    inj.end()
    inj.cancel()
    check("repeated end()/cancel() restore exactly once",
          inj._bus.swaps.count("xkb:us::eng") == 1, inj._bus.swaps)
    inj = wired()
    inj.cancel()
    check("cancel() before acquire() is a no-op", inj._bus.swaps == [], inj._bus.swaps)

    # no prior engine at all (nothing to go back to)
    inj = wired(prior=None)
    inj.acquire()
    inj.end()
    check("no prior engine: nothing is written",
          ib.read_prior_engine() is None)

    # ---- watchdog --------------------------------------------------------
    print("\n[6] watchdog bounds the session")
    saved = ib.SESSION_MAX_SECONDS
    ib.SESSION_MAX_SECONDS = 0.15
    try:
        inj = wired()
        inj.acquire()
        deadline = time.monotonic() + 3.0
        while inj._active and time.monotonic() < deadline:
            time.sleep(0.02)
        check("a session that outlives its bound is force-restored",
              inj._active is False)
        check("...and the engine went back",
              inj._bus.swaps[-1] == "xkb:us::eng", inj._bus.swaps)
        check("...and the breadcrumb was cleared", ib.read_prior_engine() is None)
    finally:
        ib.SESSION_MAX_SECONDS = saved

    # ---- crash recovery --------------------------------------------------
    print("\n[7] crash recovery replays the breadcrumb")

    class FakeIBusModule:
        def __init__(self, bus):
            self._bus = bus
            self.inited = 0

        def init(self):
            self.inited += 1

        def Bus(self):
            return self._bus

    real_ibus, real_has = ib.IBus, ib.HAS_IBUS
    try:
        bus = FakeBus(prior="gnome-speaks-stt")   # crash left OURS installed
        ib.IBus = FakeIBusModule(bus)
        ib.HAS_IBUS = True
        ib.write_prior_engine("xkb:us::eng")
        did = ib.restore_prior_engine("test")
        check("stranded engine is restored at startup", did is True)
        check("...to the recorded engine", bus.swaps == ["xkb:us::eng"], bus.swaps)
        check("...and the breadcrumb is consumed", ib.read_prior_engine() is None)

        check("no breadcrumb -> no action and no error",
              ib.restore_prior_engine("test") is False)

        bus2 = FakeBus(prior="xkb:us::eng")
        ib.IBus = FakeIBusModule(bus2)
        ib.write_prior_engine("xkb:us::eng")
        did = ib.restore_prior_engine("test")
        check("already-correct engine: no redundant swap", bus2.swaps == [], bus2.swaps)
        check("...stale breadcrumb still cleared", ib.read_prior_engine() is None)
    finally:
        ib.IBus, ib.HAS_IBUS = real_ibus, real_has

    # ---- the seam contract under IBus ------------------------------------
    print("\n[8] seam methods map correctly onto IBus")
    inj = wired()
    check("supports_preedit() is True", inj.supports_preedit() is True)

    inj = wired()
    inj.acquire()
    inj.replace_text("hello wor", "hello world")
    check("replace_text() becomes a preedit replacement",
          inj._engine.preedits == ["hello world"], inj._engine.preedits)
    check("...and commits nothing yet", inj._engine.commits == [])
    inj.send_backspaces(9)
    check("send_backspaces() clears the preedit instead of typing keys",
          inj._engine.cleared >= 1)

    inj = wired()
    inj.acquire()
    inj.paste("a block of text")
    flush_now(inj)
    check("paste() becomes a commit, not a clipboard round-trip",
          inj._engine.commits == ["a block of text"], inj._engine.commits)

    inj = wired()
    inj.acquire()
    inj.commit("one")
    inj.commit("two")
    flush_now(inj)
    check("back-to-back finals coalesce into ONE commit",
          inj._engine.commits == ["one two"], inj._engine.commits)

    inj = wired()
    got = inj.press_enter()
    check("press_enter() delegates to the key-event backend",
          inj._fallback.enters == 1, inj._fallback.enters)
    check("press_enter() never commits text", inj._engine.commits == [])
    check("press_enter() does not acquire an IBus session", inj._active is False)

    inj = wired(preedit_capable=False)
    inj.acquire()
    inj.set_preedit("partial")
    check("client without preedit capability drops partials silently",
          inj._engine.preedits == [], inj._engine.preedits)

    inj = wired()
    inj.acquire()
    inj.end()
    inj._fallback.recovers = 0
    inj.recover()
    check("recover() also clears the fallback's state", inj._fallback.recovers == 1)

    # ---- no global engine: the GNOME case that stranded a real session ---
    # Live gate finding: on GNOME the shell manages input sources and
    # GetGlobalEngine commonly fails with "No global engine". Recording None
    # there left the session stranded on gnome-speaks-stt after one cycle.
    print("\n[9] no global engine -> derive a restore target")
    real_derive = ib.derive_restore_target
    try:
        ib.derive_restore_target = lambda bus=None: "xkb:us::eng"

        for label, kwargs in (("GetGlobalEngine returns None", dict(prior=None)),
                              ("GetGlobalEngine raises", dict(prior=None,
                                                              raise_on_get=True))):
            inj = wired(**kwargs)
            ok = inj.acquire()
            check(f"{label}: acquire() still succeeds", ok is True)
            check(f"{label}: derived target recorded as the breadcrumb",
                  ib.read_prior_engine() == "xkb:us::eng", ib.read_prior_engine())
            inj.end()
            check(f"{label}: engine restored to the derived target",
                  inj._bus.swaps == [ib.ENGINE_NAME, "xkb:us::eng"], inj._bus.swaps)
            check(f"{label}: breadcrumb cleared", ib.read_prior_engine() is None)

        # late derivation: acquire found nothing, restore must still act
        inj = wired(prior=None)
        ib.derive_restore_target = lambda bus=None: None
        inj.acquire()
        check("acquire with no derivable target still proceeds", inj._active is True)
        ib.derive_restore_target = lambda bus=None: "xkb:us::eng"
        inj.end()
        check("restore derives LATE rather than standing down",
              inj._bus.swaps[-1] == "xkb:us::eng", inj._bus.swaps)

        # nothing derivable at all: must WARN, never finish quietly
        inj = wired(prior=None)
        ib.derive_restore_target = lambda bus=None: None
        buf, detach = capture_log()
        try:
            inj.acquire()
            inj.end()
        finally:
            detach()
        text = buf.getvalue()
        check("undecidable restore target logs a WARNING",
              "no restore target" in text.lower(), repr(text[-200:]))
        check("...and does not claim a restore it did not do",
              inj._bus.swaps == [ib.ENGINE_NAME], inj._bus.swaps)
    finally:
        ib.derive_restore_target = real_derive

    # ---- stranded with no breadcrumb -------------------------------------
    print("\n[10] stranded session is rescued without a breadcrumb")
    real_ibus, real_has = ib.IBus, ib.HAS_IBUS
    real_derive = ib.derive_restore_target
    try:
        class FakeIBusModule2:
            def __init__(self, bus):
                self._bus = bus

            def init(self):
                pass

            def Bus(self):
                return self._bus

        bus = FakeBus(prior=ib.ENGINE_NAME)     # we are the installed engine
        ib.IBus = FakeIBusModule2(bus)
        ib.HAS_IBUS = True
        ib.derive_restore_target = lambda b=None: "xkb:us::eng"
        ib.clear_prior_engine()                 # no breadcrumb at all
        did = ib.restore_prior_engine("test")
        check("stranded-on-ours is detected with no breadcrumb", did is True)
        check("...and the derived source is restored",
              bus.swaps[-1] == "xkb:us::eng", bus.swaps)

        bus2 = FakeBus(prior="xkb:us::eng")     # NOT ours: nothing to do
        ib.IBus = FakeIBusModule2(bus2)
        check("a session not on our engine is left alone",
              ib.restore_prior_engine("test") is False)
        check("...with no swap issued", bus2.swaps == [], bus2.swaps)
    finally:
        ib.IBus, ib.HAS_IBUS = real_ibus, real_has
        ib.derive_restore_target = real_derive

    # ---- derivation itself ------------------------------------------------
    print("\n[11] derivation reads GNOME's own input-sources")

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

    class FakeGio:
        settings = None

        @staticmethod
        def _new(schema):
            return FakeGio.settings

    class EngList:
        def __init__(self, names):
            self.names = names

        def list_engines(self):
            return [FakeEngineDesc(n) for n in self.names]

    real_gio = getattr(ib, "Gio", None)
    try:
        class GioShim:
            class Settings:
                @staticmethod
                def new(schema):
                    return FakeGio.settings
        ib.Gio = GioShim
        engines = EngList(["xkb:us::eng", "xkb:us:dvorak:eng",
                           "xkb:de:neo:deu", "xkb:de:neo:eng"])

        FakeGio.settings = FakeSettings([], [("xkb", "us")])
        check("sources[0] xkb -> the daemon's real engine name",
              ib.derive_restore_target(engines) == "xkb:us::eng")

        FakeGio.settings = FakeSettings([("xkb", "us+dvorak")], [("xkb", "us")])
        check("mru-sources wins over sources",
              ib.derive_restore_target(engines) == "xkb:us:dvorak:eng")

        FakeGio.settings = FakeSettings([], [("ibus", "anthy")])
        check("an ibus source is already an engine name",
              ib.derive_restore_target(engines) == "anthy")

        FakeGio.settings = FakeSettings([], [])
        check("no configured sources -> None (caller warns)",
              ib.derive_restore_target(engines) is None)
    finally:
        if real_gio is not None:
            ib.Gio = real_gio

    # ---- we never touched the live session -------------------------------
    print("\n[12] live session untouched")
    check("no real prior-engine file outside the redirected runtime dir",
          ib.prior_engine_path().startswith(RUNTIME), ib.prior_engine_path())

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("ALL IBUS BACKEND CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
