#!/usr/bin/env python3
"""COMPOSITION: the injector pin (#46) x the latched dead-recorder verdict (#57/#72).

Neither suite can see this case on its own. lucid-dead-recorder never swaps the
engine, so it only ever exercises one backend; lucid-pin-lifecycle never kills
the recorder, so it never reaches the latched-verdict exit. The interesting
question lives exactly where they overlap:

    the mic is yanked mid-utterance, AFTER a "cast typing engine" swap

Three guarantees have to survive together:

  X1  #46: the words go to the backend that TYPED the live partial (the pin),
      not to whatever get_injector() returns after the swap.
  X2  #57: the dead mic is still reported -- exactly once, and IN ADDITION to
      the text, never instead of it.
  X3  #46 lifecycle: the pinned backend is released, and released AFTER it
      delivered.  The release lives in the post-loop `finally`; the dead-mic
      report is emitted after that `finally`.  If the release were ever hoisted
      above the delivery, or swapped for cancel() (which DISCARDS rather than
      flushes), the user would lose the words the report is promising it kept.

exit 0 = all three hold, exit 1 = at least one is violated.
"""
import logging
import atexit
import shutil
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "dead-recorder"))
import fakes      # noqa: E402
import harness    # noqa: E402

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
SCRATCH = os.environ.get("GS_REPRO_SCRATCH") or os.path.join(SCRATCH_ROOT, f"pin-x-deadmic-{os.getpid()}")
os.makedirs(SCRATCH, exist_ok=True)
if _OWNED:
    atexit.register(shutil.rmtree, SCRATCH, True)

WINDOW = 8.0
PHRASE = "the words the report promises it kept"


class Backend:
    """Records what reached the cursor and how the session was closed."""

    def __init__(self, name):
        self.name = name
        self.ops = []        # [(op, text, monotonic)]
        self.lifecycle = []

    def prepare(self): return True
    def acquire(self): self.lifecycle.append(("acquire", time.monotonic())); return True
    def end(self): self.lifecycle.append(("end", time.monotonic())); return True
    def cancel(self): self.lifecycle.append(("cancel", time.monotonic())); return True
    def recover(self): return True
    def available(self): return True
    def purpose_known(self): return False
    def supports_preedit(self): return False

    def _op(self, op, text=""):
        self.ops.append((op, text, time.monotonic())); return True

    def commit(self, t): return self._op("commit", t)
    def type_text(self, t): return self._op("type_text", t)
    def paste(self, t): return self._op("paste", t)
    def finalize(self, t): return self._op("finalize", t)
    def set_preedit(self, t): return self._op("set_preedit", t)
    def replace_text(self, o, nw): return self._op("replace_text", nw)
    def send_backspaces(self, n): return self._op("send_backspaces", "")
    def press_enter(self): return self._op("press_enter")

    DELIVERY = ("commit", "paste", "finalize", "type_text")

    def delivered_text(self):
        return [t for op, t, _ in self.ops if op in self.DELIVERY and t]

    def deliveries_at(self):
        return [ts for op, _, ts in self.ops if op in self.DELIVERY]

    def ends(self):
        return [ts for op, ts in self.lifecycle if op == "end"]


def main():
    mod, _events, _rec = harness.load()
    print(f"service under test: {harness.SVC_PATH}")

    A, B = Backend("ydotool"), Backend("ibus")
    registry = {"ydotool": A, "ibus": B}
    cur = {"obj": A, "method": "ydotool"}
    lock = threading.Lock()

    def get_injector():
        m = str(mod.CONFIG.get("injection_method") or "ydotool").lower()
        with lock:
            if m != cur["method"]:
                cur["obj"].cancel()          # all the real rebuild does
                cur["obj"] = registry.get(m, A)
                cur["method"] = m
            return cur["obj"]
    mod.get_injector = get_injector

    # Pin every flag the verdict rests on.
    mod.CONFIG["injection_method"] = "ydotool"
    mod.CONFIG["dictation_mode"] = True
    mod.CONFIG["conversation_mode"] = False
    mod.CONFIG["continuous_dictation"] = True     # loop: the mic-yank case JP hits
    mod.CONFIG["skip_final_paste"] = False
    mod.CONFIG["wake_word_secure_gate"] = False

    errors = []
    mod.GLib.idle_add = lambda fn, *a, **k: fn(*a, **k)

    # A recorder that stays alive until we yank it, then behaves like pw-record:
    # EOF and a non-None poll() arrive together.
    class Yankable:
        def __init__(self):
            self.dead = threading.Event()
            self.exited = False
            outer = self

            class Out:
                def read(self, n):
                    if outer.dead.is_set():
                        outer.exited = True
                        return b""
                    time.sleep(0.01)
                    return b"\x00" * n
            self.stdout = Out()

        def poll(self): return 1 if self.exited else None
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): return 1

    proc = Yankable()

    class WS:
        def settimeout(self, t): pass
        def send(self, p, opcode=None): pass
        def recv(self):
            time.sleep(0.02)
            return "MSG"

    mod.HAS_WS = True
    mod.HAS_VAD = False
    mod._take_prewarmed_rec = lambda *a, **k: proc
    mod._get_stt_ws = lambda *a, **k: (WS(), True)
    mod._init_stt_ws_session = lambda *a, **k: None
    mod._make_ws_audio_msg = lambda *a, **k: b""
    mod.calibrate_noise = lambda p: (1000.0, [])
    mod.rms_energy = lambda c: 5000.0
    mod.is_speech_energy = lambda c, v, t: True
    mod._rest_stt_fallback = lambda *a, **k: ""
    mod._invalidate_stt_ws = lambda *a, **k: None

    swapped = threading.Event()
    step = {"n": 0}
    served = []

    def fake_parse(msg, phrases, partial_holder, end_word_event, end_word,
                   _log, raw_partial_holder=None, use_lexical=False):
        step["n"] += 1
        if step["n"] == 1:
            partial_holder[0] = PHRASE
            if raw_partial_holder is not None:
                raw_partial_holder[0] = PHRASE
            return "hypothesis"
        if step["n"] == 2:
            # The live partial is really on screen through the PINNED backend.
            harness.wait_for(lambda: bool(A.ops), 3.0)
            # ... now the user casts the engine swap ...
            box["svc"]._spell_ctx_dbus("injection_toggle")
            swapped.set()
            return "hypothesis"
        # ... and THEN the USB mic is yanked.
        # Append ONCE: the post-sender drain loop parses again after the
        # receive loop breaks, and a second append would double the transcript
        # (`user_text = " ".join(phrases)`) and read as "text was mangled".
        if not served:
            served.append(True)
            phrases.append(PHRASE)
            partial_holder[0] = PHRASE
            proc.dead.set()
            return "turn_end"
        return None

    mod._parse_ws_msg = fake_parse

    box = {}
    svc = harness.make_service(mod)
    box["svc"] = svc
    svc._stt_mode = "streaming"
    svc._emit_error = lambda m: errors.append(m)

    assert svc.start_listening() == "ok", "setup: start_listening refused"
    assert swapped.wait(WINDOW), "setup: the engine swap never fired"
    harness.wait_for(lambda: svc.current_state == "idle", WINDOW)
    time.sleep(0.5)

    print(f"  A(ydotool) ops={[o for o, _, _ in A.ops]} "
          f"lifecycle={[o for o, _ in A.lifecycle]}")
    print(f"  B(ibus)    ops={[o for o, _, _ in B.ops]} "
          f"lifecycle={[o for o, _ in B.lifecycle]}")
    print(f"  Error signals: {errors}")

    failures = 0

    # X1 -- the words went to the backend that typed them
    if PHRASE in B.delivered_text():
        failures += 1
        print("    FAIL (X1): the transcript was delivered by the NEW backend "
              "after the swap -- #46, on the dead-mic exit")
    elif PHRASE not in A.delivered_text():
        failures += 1
        print(f"    FAIL (X1): the transcript never reached the cursor at all "
              f"(A delivered {A.delivered_text()}) -- the dead recorder cost "
              f"the user their words")
    else:
        print("    OK (X1): the words were delivered by the pinned backend")

    # X2 -- the mic is still reported, exactly once, IN ADDITION to the text
    mic = [e for e in errors if "icrophone" in e]
    if len(mic) != 1:
        failures += 1
        print(f"    FAIL (X2): expected exactly one microphone Error, got "
              f"{len(mic)}: {mic}")
    else:
        print("    OK (X2): the lost microphone was reported exactly once")

    # X3 -- released, and released after delivering
    d = A.deliveries_at()
    if not A.ends():
        failures += 1
        print("    FAIL (X3): the pinned backend was never released -- it "
              "re-acquired on the post-swap delivery and holds the input "
              "method until SESSION_MAX_SECONDS")
    elif d and not any(ts > d[-1] for ts in A.ends()):
        failures += 1
        print("    FAIL (X3): the pinned backend was released BEFORE its last "
              "delivery -- a coalesced commit would be truncated, and the "
              "dead-mic report would be promising words that never landed")
    else:
        print("    OK (X3): released, and only after the words were delivered")

    print()
    if failures:
        print(f"FAIL: {failures} -- the pin and the dead-mic verdict do not compose")
        return 1
    print("PASS: words to the pinned backend, mic reported once, pin released "
          "after delivery")
    return 0


if __name__ == "__main__":
    sys.exit(main())
