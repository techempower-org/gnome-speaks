#!/usr/bin/env python3
"""#87: the AI-reply <type> paste resolves get_injector() inside the pinned scope.

Drives the REAL _streaming_stt_cycle in conversation mode -- not the reply
worker on its own thread. That distinction is the whole point:

  * production calls `self._conversation_worker(user_text)` SYNCHRONOUSLY from
    inside the cycle (the line above is `inj.send_backspaces(...)`), so the
    cycle's `finally` -- and its `inj.end()` from #61 -- runs AFTER the reply
    finishes;
  * every existing reply repro (subtitle e3/e4, begin-refused j/k/l) starts the
    worker on its OWN thread, which does not have that release at all.

A worker-only test therefore reports a strand that production does not have.
This one keeps the cycle in the picture so the verdict is about the service.

The window under test, on d92166c:

    4583   get_injector().paste(type_text)          <- resolution #1
    4588   if spoke_anything and half_duplex: sleep(0.5)
    4591   self._set_state("idle") -> 1935 get_injector().end()   <- #2

Assertions, per backend:
  R1  the <type> paste landed on the backend the cycle PINNED
  R2  every backend this utterance ACQUIRED was released (end or cancel)
  R3  exactly one release of the backend that took the paste

exit 0 = clean, exit 1 = at least one violated.
"""
import logging
import atexit
import shutil
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "cancel-tokens"))
import harness  # noqa: E402

logging.disable(logging.INFO)
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

# Clean up on exit, but ONLY a directory this process chose. repro_pin_lifecycle
# forks a child per scenario with GS_REPRO_SCRATCH pointing INTO its own
# scratch, so a child that cleaned up a caller-pinned path would delete the
# still-running parent's tree out from under it -- the same reason the shared
# harnesses read this variable with get() and never setdefault().
_OWNED = "GS_REPRO_SCRATCH" not in os.environ
SCRATCH = os.environ.get("GS_REPRO_SCRATCH") or os.path.join(SCRATCH_ROOT, f"reply-pin-{os.getpid()}")
os.makedirs(SCRATCH, exist_ok=True)
if _OWNED:
    atexit.register(shutil.rmtree, SCRATCH, True)

REPLY = "Sure. <type>print('hello')</type>"


class Backend:
    def __init__(self, name):
        self.name = name
        self.acquired = False
        self.ops = []
        self.lifecycle = []

    def prepare(self): return True

    def acquire(self):
        self.acquired = True
        self.lifecycle.append(("acquire", time.monotonic())); return True

    def end(self):
        self.acquired = False
        self.lifecycle.append(("end", time.monotonic())); return True

    def cancel(self):
        self.acquired = False
        self.lifecycle.append(("cancel", time.monotonic())); return True

    def recover(self): return True
    def available(self): return True
    def purpose_known(self): return False
    def supports_preedit(self): return False

    def _op(self, op, text=""):
        self.acquired = True                    # every text op re-acquires
        self.ops.append((op, text, time.monotonic())); return True

    def commit(self, t): return self._op("commit", t)
    def type_text(self, t): return self._op("type_text", t)
    def paste(self, t): return self._op("paste", t)
    def finalize(self, t): return self._op("finalize", t)
    def set_preedit(self, t): return self._op("set_preedit", t)
    def replace_text(self, o, n): return self._op("replace_text", n)
    def send_backspaces(self, n): return self._op("send_backspaces")
    def press_enter(self): return self._op("press_enter")

    def pasted(self):
        return [t for op, t, _ in self.ops if op == "paste"]

    def releases(self):
        return [ts for op, ts in self.lifecycle if op in ("end", "cancel")]


class FakeStdout:
    def __init__(self, n): self._f = b"\x00" * n
    def read(self, n):
        time.sleep(0.005); return self._f


class FakeProc:
    def __init__(self, n): self.stdout = FakeStdout(n)
    def terminate(self): pass
    def kill(self): pass
    def poll(self): return None
    def wait(self, timeout=None): return 0


class FakeWS:
    def __init__(self): self.closed = threading.Event()
    def settimeout(self, t): pass
    def send(self, p, opcode=None): pass
    def recv(self):
        if self.closed.is_set(): raise RuntimeError("closed")
        time.sleep(0.02); return "MSG"


def main():
    mod, _events, _rec = harness.load()
    print(f"service under test: {harness.SVC_PATH}")

    A, B, C = Backend("ydotool"), Backend("ibus"), Backend("ydotool2")
    reg = {"ydotool": A, "ibus": B, "ydotool2": C}
    cur = {"obj": A, "method": "ydotool"}
    lk = threading.Lock()

    def get_injector():
        m = str(mod.CONFIG.get("injection_method") or "ydotool").lower()
        with lk:
            if m != cur["method"]:
                cur["obj"].cancel()          # the rebuild's only cleanup
                cur["obj"] = reg.get(m, A)
                cur["method"] = m
            return cur["obj"]
    mod.get_injector = get_injector

    mod.CONFIG["injection_method"] = "ydotool"
    mod.CONFIG["dictation_mode"] = True
    mod.CONFIG["conversation_mode"] = True      # the reply path
    mod.CONFIG["continuous_dictation"] = False
    mod.CONFIG["half_duplex"] = True            # opens the 0.5 s window
    mod.CONFIG["terminal_mode"] = True          # <type> tags are terminal-mode
    mod.CONFIG["wake_word_secure_gate"] = False

    # The reply: one sentence spoken, one <type> tag typed.
    mod.stream_chat = lambda **kw: iter(["Sure. ", "<type>print('hello')</type>"])

    proc, ws = FakeProc(mod.FRAME_BYTES), FakeWS()
    mod.HAS_WS = True
    mod.HAS_VAD = False
    mod._take_prewarmed_rec = lambda *a, **k: proc
    mod._get_stt_ws = lambda *a, **k: (ws, True)
    mod._init_stt_ws_session = lambda *a, **k: None
    mod._make_ws_audio_msg = lambda *a, **k: b""
    mod.calibrate_noise = lambda p: (1000.0, [])
    mod.rms_energy = lambda c: 5000.0
    mod.is_speech_energy = lambda c, v, t: True
    mod._rest_stt_fallback = lambda *a, **k: ""
    mod._invalidate_stt_ws = lambda *a, **k: None

    served = []

    def fake_parse(msg, phrases, partial_holder, end_word_event, end_word,
                   _log, raw_partial_holder=None, use_lexical=False):
        if not served:
            served.append(True)
            phrases.append("write me a hello world")
            partial_holder[0] = "write me a hello world"
            return "turn_end"
        return None
    mod._parse_ws_msg = fake_parse

    # THE EVENT UNDER TEST: the user casts "typing engine" while the reply is
    # typing -- placed deterministically inside the paste itself, which is the
    # top of the 0.5 s half-duplex window between the two resolutions.
    swaps = {"n": 0}
    orig_paste = {}
    for be in (A, B, C):
        orig_paste[be.name] = be.paste

    def make_paste(be):
        def paste(t):
            r = orig_paste[be.name](t)
            if swaps["n"] == 0:                 # first paste only
                swaps["n"] += 1
                mod.CONFIG["injection_method"] = "ibus"
            return r
        return paste
    for be in (A, B, C):
        be.paste = make_paste(be)

    svc = harness.make_service(mod)
    svc._stt_mode = "streaming"
    assert svc.start_listening() == "ok", "setup: start_listening refused"
    harness.wait_for(lambda: svc.current_state == "idle", 25.0)
    time.sleep(0.8)
    ws.closed.set()

    for be in (A, B, C):
        ops = str([o for o, _, _ in be.ops])
        print(f"  {be.name:9s} ops={ops:<44} "
              f"lifecycle={[o for o, _ in be.lifecycle]} acquired={be.acquired}")
    print(f"  swaps fired: {swaps['n']}")

    failures = 0
    pasters = [be for be in (A, B, C) if be.pasted()]
    if not pasters:
        print("    SETUP FAILURE: the <type> paste never happened at all")
        return 2
    if swaps["n"] == 0:
        print("    SETUP FAILURE: the engine swap never fired")
        return 2

    paster = pasters[0]
    # R1 -- the paste belongs to the backend the cycle pinned (A: pinned before
    # the swap, and the swap only fires DURING the paste).
    if paster is not A:
        failures += 1
        print(f"    FAIL (R1): the <type> paste landed on {paster.name}, not on "
              f"the pinned backend")
    else:
        print("    OK (R1): the <type> paste landed on the pinned backend")

    # R2 -- nothing this utterance acquired may be left holding the IME.
    stranded = [be.name for be in (A, B, C) if be.acquired]
    if stranded:
        failures += 1
        print(f"    FAIL (R2): {stranded} left acquired -- holds the input "
              f"method until SESSION_MAX_SECONDS=120")
    else:
        print("    OK (R2): every backend this utterance touched was released")

    # R3 -- the paster is released, after its paste.
    last = paster.ops[-1][2]
    after = [ts for ts in paster.releases() if ts > last]
    if not after:
        failures += 1
        print(f"    FAIL (R3): {paster.name} was never released after its paste")
    else:
        print(f"    OK (R3): {paster.name} released after its paste")

    print()
    if failures:
        print(f"FAIL: {failures} -- the reply path and the cycle's pin do not compose")
        return 1
    print("PASS: paste on the pinned backend, nothing left holding the IME")
    return 0


if __name__ == "__main__":
    sys.exit(main())
