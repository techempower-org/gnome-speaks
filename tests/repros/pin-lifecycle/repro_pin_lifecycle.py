#!/usr/bin/env python3
"""Issue #46 / PR #61: the per-cycle injector pin must also be RELEASED.

PR #61 pins `inj = get_injector()` once per STT cycle so a mid-utterance
"cast typing engine" cannot retract with the wrong backend.  That half is
necessary and this repro guards it (P1a/P2a).  But pinning alone strands an
input method:

  * `get_injector()` rebuilds on a config flip and only `cancel()`s the
    outgoing backend.
  * A cancelled `IbusInjector` RE-ACQUIRES on its next replace_text /
    type_text / commit -- and the cycle keeps typing through the pin, so it
    does exactly that.
  * The only `end()` on the path is the idle hook
    (`_set_state('idle')` -> `get_injector().end()`), which ends whatever is
    CURRENT.  After a swap that is a different object from the pin.

  => the swapped-away backend holds the user's input method until
     `SESSION_MAX_SECONDS = 120` (ibus_injector.py).  Per CLAUDE.md that is
     the "no input method at all" hazard, not merely "the wrong one".

Assertions
  P1  single-shot, swap mid-utterance (the deterministic POST /cast trigger)
      a. every text op for the utterance went to the PINNED backend
      b. the pinned backend was end()ed -- not left acquired
  P2  loop mode, swap during cycle 1 (the racy spoken-cast trigger:
      _drain_speech_gap returns instantly on an empty queue, so the next
      cycle re-pins right as the spell thread flips CONFIG)
      a. cycle 1's text ops went to the pinned backend
      b. the outgoing backend was end()ed AT THE RE-PIN -- strictly before
         cycle 2 put any text through the new one
      c. no backend is left acquired-but-not-ended when the worker returns

Run:  GS_SVC_PATH=<path to a gnome-speaks-service.py> python3 repro_pin_lifecycle.py
exit 0 = clean, exit 1 = bug present.
"""
import logging
import atexit
import shutil
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "cancel-tokens"))
import harness  # noqa: E402

logging.disable(logging.INFO)  # the service is chatty; assertions are the output

# Scratch isolation, same idiom as the other suites (2026-09-06 sweep): get(),
# never setdefault -- setdefault EXPORTS the value, so a scenario child would
# inherit the PARENT's directory and the per-PID isolation would collapse to a
# shared path. The parent passes it down deliberately instead (see main()).
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
SCRATCH = os.environ.get("GS_REPRO_SCRATCH") or os.path.join(SCRATCH_ROOT, f"pin-lifecycle-{os.getpid()}")
os.makedirs(SCRATCH, exist_ok=True)
if _OWNED:
    atexit.register(shutil.rmtree, SCRATCH, True)


class FakeBackend:
    """One injection backend, with the lifecycle facts we need to assert on.

    Models the two properties that make the leak real:
      * cancel() drops the session (that is all the rebuild does)
      * ANY text op re-acquires it   (IbusInjector.commit -> acquire())
    """

    def __init__(self, name, preedit):
        self.name = name
        self._preedit = preedit
        self.acquired = False
        self.ops = []        # [(op, monotonic)] -- text ops only
        self.lifecycle = []  # [(op, monotonic)] -- acquire/end/cancel

    # -- lifecycle ---------------------------------------------------------
    def prepare(self):
        return True

    def acquire(self):
        self.acquired = True
        self.lifecycle.append(("acquire", time.monotonic()))
        return True

    def end(self):
        self.acquired = False
        self.lifecycle.append(("end", time.monotonic()))
        return True

    def cancel(self):
        self.acquired = False
        self.lifecycle.append(("cancel", time.monotonic()))
        return True

    def recover(self):
        return True

    def available(self):
        return True

    def purpose_known(self):
        return False

    def supports_preedit(self):
        return self._preedit

    # -- text ops (every one of these re-acquires, like IbusInjector) ------
    def _text(self, op):
        self.acquired = True
        self.ops.append((op, time.monotonic()))
        return True

    def commit(self, text):
        return self._text("commit")

    def type_text(self, text):
        return self._text("type_text")

    def paste(self, text):
        return self._text("paste")

    def set_preedit(self, text):
        return self._text("set_preedit")

    def replace_text(self, old, new):
        return self._text("replace_text")

    def send_backspaces(self, count):
        return self._text("send_backspaces")

    def finalize(self, text):
        return self._text("finalize")

    def press_enter(self):
        return self._text("press_enter")

    # -- assertions --------------------------------------------------------
    def first_op_at(self):
        return self.ops[0][1] if self.ops else None

    def ends(self):
        return [t for op, t in self.lifecycle if op == "end"]

    DELIVERY = ("commit", "paste", "finalize", "type_text")

    def deliveries(self):
        """Ops that actually put final text in the field. On IBus these are
        COALESCED -- end() flushes the buffer (`_flush(allowed=True)`), while
        cancel() discards it -- so when they happen relative to end()/cancel()
        is the whole question."""
        return [t for op, t in self.ops if op in self.DELIVERY]

    def end_before_delivery(self):
        """An end() landing between the first and last delivery would flush a
        half-finished utterance; one landing before any delivery on a backend
        that then delivers means the release was hoisted above the text."""
        d = self.deliveries()
        if not d:
            return False
        return any(d[0] <= t < d[-1] for t in self.ends())

    def released_after_delivery(self):
        d = self.deliveries()
        return bool(d) and any(t > d[-1] for t in self.ends())

    def stranded(self):
        """Acquired, and no end() after the last text op."""
        if not self.ops:
            return False
        last_op = self.ops[-1][1]
        return self.acquired and not any(t > last_op for t in self.ends())


class FakeStdout:
    def __init__(self, frame_bytes):
        self._frame = b"\x00" * frame_bytes

    def read(self, n):
        time.sleep(0.005)
        return self._frame


class FakeProc:
    def __init__(self, frame_bytes):
        self.stdout = FakeStdout(frame_bytes)
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def poll(self):
        return 0 if self.terminated else None

    def wait(self, timeout=None):
        return 0


class FakeWS:
    def __init__(self):
        self.closed = threading.Event()

    def settimeout(self, t):
        pass

    def send(self, payload, opcode=None):
        if self.closed.is_set():
            raise RuntimeError("ws closed")

    def recv(self):
        if self.closed.is_set():
            raise RuntimeError("ws closed")
        time.sleep(0.02)
        return "MSG"


def build(loop_mode):
    """Wire the real service up to fake audio/WS and a two-backend registry."""
    mod, _events, _rec = harness.load()

    A = FakeBackend("ydotool", preedit=False)
    B = FakeBackend("ibus", preedit=True)
    registry = {"ydotool": A, "ibus": B}
    cur = {"obj": A, "method": "ydotool"}
    reg_lock = threading.Lock()

    def get_injector():
        """Mirrors the real one: rebuild on method change, cancel() the old."""
        method = str(mod.CONFIG.get("injection_method") or "ydotool").lower()
        with reg_lock:
            if method != cur["method"]:
                cur["obj"].cancel()          # the ONLY thing the rebuild does
                cur["obj"] = registry.get(method, A)
                cur["method"] = method
            return cur["obj"]

    mod.get_injector = get_injector
    # Real _save_config_flag semantics (CONFIG[key] = value) with the disk
    # write removed: a repro must NEVER touch ~/.config/speech-to-cli/config.json.
    def _save_flag(self, key, value):
        mod.CONFIG[key] = value
    mod.GnomeSpeaksService._save_config_flag = _save_flag
    # ...and never let it READ that file back over the scenario's flags:
    # start_listening() calls _reload_config_flags(), which copies every
    # _SYNC_FLAGS key off disk into CONFIG. Without this stub the repro runs
    # against whatever mode JP's desktop happens to be in -- measured: it
    # silently reset continuous_dictation and the loop scenario never looped.
    mod.GnomeSpeaksService._reload_config_flags = lambda self, *a, **k: None
    # Pin every flag this repro's verdict depends on. harness.load() already
    # neutralizes the CONFIG_PATH read, but the verdict must not rest on that.
    mod.CONFIG["injection_method"] = "ydotool"
    mod.CONFIG["dictation_mode"] = True
    mod.CONFIG["conversation_mode"] = False
    mod.CONFIG["continuous_dictation"] = loop_mode
    mod.CONFIG["skip_final_paste"] = False
    mod.CONFIG["wake_word_secure_gate"] = False

    proc = FakeProc(mod.FRAME_BYTES)
    ws = FakeWS()
    mod.HAS_WS = True
    mod.HAS_VAD = False
    mod._take_prewarmed_rec = lambda *a, **k: proc
    mod._get_stt_ws = lambda *a, **k: (ws, True)
    mod._init_stt_ws_session = lambda *a, **k: None
    mod._make_ws_audio_msg = lambda *a, **k: b""
    mod.calibrate_noise = lambda p: (1000.0, [])
    mod.rms_energy = lambda chunk: 5000.0
    mod.is_speech_energy = lambda chunk, vad, thr: True
    mod._rest_stt_fallback = lambda *a, **k: ""
    return mod, ws, A, B


def cast_typing_engine(svc):
    """Fire the real spell the way /cast and a spoken cast do: on another
    thread, and note that _spell_ctx_dbus('injection_toggle') itself calls
    get_injector() to compose its reply -- so the REBUILD (and the outgoing
    backend's cancel()) lands before the STT cycle types another character."""
    box = {}

    def go():
        box["reply"] = svc._spell_ctx_dbus("injection_toggle")

    t = threading.Thread(target=go, name="spell-thread")
    t.start()
    t.join(5.0)
    return box.get("reply")


def run_p1():
    """Single-shot: swap the engine mid-utterance, after live text is typed."""
    mod, ws, A, B = build(loop_mode=False)
    swapped = threading.Event()
    step = {"n": 0}
    box = {}

    def fake_parse(msg, phrases, partial_holder, end_word_event, end_word,
                   _log, raw_partial_holder=None, use_lexical=False):
        step["n"] += 1
        if step["n"] == 1:
            partial_holder[0] = "alpha"
            if raw_partial_holder is not None:
                raw_partial_holder[0] = "alpha"
            return "hypothesis"
        if step["n"] == 2:
            # Wait until the live typer really put text on screen through the
            # pinned backend -- that is what makes the retract ambiguous.
            harness.wait_for(lambda: bool(A.ops), 3.0)
            cast_typing_engine(box["svc"])       # <= POST /cast typing-engine
            swapped.set()
            return "hypothesis"
        phrases.append("alpha beta")
        partial_holder[0] = "alpha beta"
        return "turn_end"

    mod._parse_ws_msg = fake_parse
    svc = harness.make_service(mod)
    box["svc"] = svc
    svc._stt_mode = "streaming"
    assert svc.start_listening() == "ok", "setup: start_listening refused"
    assert swapped.wait(6.0), "setup: the engine swap never fired"
    harness.wait_for(lambda: svc.current_state == "idle", 8.0)
    time.sleep(0.4)
    ws.closed.set()
    return A, B


class MinimalBackend:
    """A backend that implements the seam and nothing more -- notably NO
    `name`, which the Injector base supplies a default for precisely so a
    partial backend degrades instead of crashing (injector.py).

    A real one arrived from the offline-handoff suite's `_Inj`, and it found a
    genuine bug: an unguarded `inj.name` in the re-pin's DIAGNOSTIC took the
    whole STT cycle down with an AttributeError -- 0 frames to the recognizer,
    nothing typed, on the ordinary healthy-Azure path.
    """

    def prepare(self): return True
    def acquire(self): return True
    def end(self): return True
    def cancel(self): return True
    def recover(self): return True
    def available(self): return True
    def purpose_known(self): return False
    def supports_preedit(self): return False

    def __init__(self): self.text = []
    def commit(self, t): self.text.append(t); return True
    def type_text(self, t): return True
    def paste(self, t): self.text.append(t); return True
    def finalize(self, t): self.text.append(t); return True
    def set_preedit(self, t): return True
    def replace_text(self, o, n): return True
    def send_backspaces(self, n): return True
    def press_enter(self): return True


def run_p3():
    """Swapping TO a backend with no `name` must not crash the cycle.

    Config-driven flip (what prefs.js does when it writes injection_method),
    not the spell -- `_spell_ctx_dbus` reads `get_injector().name` itself, so
    routing through it would be measuring that line rather than the re-pin.
    """
    # LOOP mode: the re-pin runs at the TOP of a cycle, so a single-shot
    # session never reaches it and would "pass" without executing the line
    # under test at all. (Measured: the single-shot version of this scenario
    # passed against the unguarded build.)
    mod, ws, A, _B = build(loop_mode=True)
    partial = MinimalBackend()
    real_get = mod.get_injector

    def get_injector():
        if str(mod.CONFIG.get("injection_method") or "").lower() == "ibus":
            return partial
        return real_get()
    mod.get_injector = get_injector

    cycles = {}
    steps = {}
    done = set()             # cycles whose phrase has already been appended
    swapped = threading.Event()

    def fake_parse(msg, phrases, partial_holder, end_word_event, end_word,
                   _log, raw_partial_holder=None, use_lexical=False):
        key = id(phrases)
        if key not in cycles:
            cycles[key] = len(cycles) + 1
            steps[key] = 0
        cyc = cycles[key]
        steps[key] += 1
        idx = steps[key]
        if idx == 1:
            partial_holder[0] = f"cycle {cyc} alpha"
            if raw_partial_holder is not None:
                raw_partial_holder[0] = f"cycle {cyc} alpha"
            return "hypothesis"
        if cyc == 1 and idx == 2:
            harness.wait_for(lambda: bool(A.ops), 3.0)
            mod.CONFIG["injection_method"] = "ibus"    # prefs.js writes the key
            swapped.set()
            return "hypothesis"
        if steps[key] > 90 or key in done:
            return None            # drain loop: do not append twice
        done.add(key)
        if cyc >= 2:
            mod.CONFIG["continuous_dictation"] = False
        phrases.append(f"cycle {cyc} beta")
        partial_holder[0] = f"cycle {cyc} beta"
        return "turn_end"

    mod._parse_ws_msg = fake_parse
    svc = harness.make_service(mod)
    svc._stt_mode = "streaming"
    errors = []
    svc._emit_error = lambda m: errors.append(m)
    assert svc.start_listening() == "ok", "setup: start_listening refused"
    assert swapped.wait(6.0), "setup: the swap never fired"
    harness.wait_for(lambda: len(cycles) >= 2, 10.0)
    harness.wait_for(lambda: svc.current_state == "idle", 10.0)
    time.sleep(0.4)
    ws.closed.set()
    return A, partial, errors, len(cycles)


def run_p2():
    """Loop mode: swap during cycle 1; cycle 2 must re-pin AND release."""
    mod, ws, A, B = build(loop_mode=True)
    swapped = threading.Event()
    cycles = {}      # id(phrases) -> cycle number
    steps = {}       # id(phrases) -> messages delivered this cycle
    order = []
    marks = {}       # 'swap' / 'cycle2' -> monotonic, for window assertions
    box = {}

    def fake_parse(msg, phrases, partial_holder, end_word_event, end_word,
                   _log, raw_partial_holder=None, use_lexical=False):
        key = id(phrases)
        if key not in cycles:
            cycles[key] = len(cycles) + 1
            steps[key] = 0
            if cycles[key] == 2:
                marks["cycle2"] = time.monotonic()
        cyc = cycles[key]
        steps[key] += 1
        idx = steps[key]
        order.append((cyc, idx))

        if idx == 1:
            partial_holder[0] = f"cycle {cyc} alpha"
            if raw_partial_holder is not None:
                raw_partial_holder[0] = f"cycle {cyc} alpha"
            return "hypothesis"
        if cyc == 1 and idx == 2:
            # The live text is really on screen through the pinned backend --
            # NOW flip the engine, exactly as the spoken cast's thread does.
            harness.wait_for(lambda: bool(A.ops), 3.0)
            marks["swap"] = time.monotonic()
            cast_typing_engine(box["svc"])       # <= the spoken cast lands
            swapped.set()
            return "hypothesis"
        if cyc >= 2:
            mod.CONFIG["continuous_dictation"] = False   # unwind after cycle 2
        phrases.append(f"cycle {cyc} beta")
        partial_holder[0] = f"cycle {cyc} beta"
        return "turn_end"

    mod._parse_ws_msg = fake_parse
    svc = harness.make_service(mod)
    box["svc"] = svc
    svc._stt_mode = "streaming"
    assert svc.start_listening() == "ok", "setup: start_listening refused"
    assert swapped.wait(6.0), "setup: the engine swap never fired"
    harness.wait_for(lambda: len(cycles) >= 2, 10.0)
    harness.wait_for(lambda: svc.current_state == "idle", 10.0)
    time.sleep(0.4)
    ws.closed.set()
    return A, B, len(cycles), marks


def scenario_p1():
    failures = 0
    print("  P1: single-shot, engine swapped mid-utterance")
    A, B = run_p1()
    print(f"    A(ydotool) ops={[o for o, _ in A.ops]} "
          f"lifecycle={[o for o, _ in A.lifecycle]}")
    print(f"    B(ibus)    ops={[o for o, _ in B.ops]} "
          f"lifecycle={[o for o, _ in B.lifecycle]}")
    if not A.ops:
        failures += 1
        print("    FAIL (P1a): nothing was typed through the pinned backend")
    elif B.ops:
        failures += 1
        print(f"    FAIL (P1a): {len(B.ops)} op(s) ({[o for o, _ in B.ops]}) went "
              f"to the NEW backend mid-utterance -- this is #46, the "
              f"wrong-backend retraction")
    else:
        print("    OK (P1a): the whole utterance stayed on the pinned backend")
    if not A.ends():
        failures += 1
        print("    FAIL (P1b): the pinned backend was never end()ed. It was "
              "cancel()ed by the rebuild, then RE-ACQUIRED by the retract "
              "(_ensure_session -> acquire), and now holds the input method "
              "until SESSION_MAX_SECONDS=120s")
    elif A.stranded():
        failures += 1
        print("    FAIL (P1b): pinned backend still acquired after its last op")
    else:
        print("    OK (P1b): the pinned backend was handed back")
    failures += check_release_ordering("P1c", A, B)
    return failures


def scenario_p2():
    failures = 0
    print("  P2: loop mode, engine swapped during cycle 1")
    A, B, ncycles, marks = run_p2()
    print(f"    cycles observed: {ncycles}")
    print(f"    A(ydotool) ops={[o for o, _ in A.ops]} "
          f"lifecycle={[o for o, _ in A.lifecycle]}")
    print(f"    B(ibus)    ops={[o for o, _ in B.ops]} "
          f"lifecycle={[o for o, _ in B.lifecycle]}")
    if ncycles < 2:
        print("    FAIL (setup): the loop never reached a second cycle")
        return failures + 1
    swap_at, cycle2_at = marks.get("swap"), marks.get("cycle2")
    # Cycle 1's FINAL ops land after the swap but before cycle 2 opens. That
    # window is the whole question: they belong to the backend cycle 1 pinned.
    a_after_swap = [o for o, t in A.ops if swap_at and t > swap_at]
    b_before_c2 = [o for o, t in B.ops if cycle2_at and t < cycle2_at]
    if not A.ops:
        failures += 1
        print("    FAIL (P2a): cycle 1 typed nothing through the pin")
    elif b_before_c2:
        failures += 1
        print(f"    FAIL (P2a): cycle 1's final ops {b_before_c2} went to the "
              f"NEW backend -- the mid-utterance swap reached the cursor (#46)")
    elif not a_after_swap:
        failures += 1
        print("    FAIL (P2a): nothing reached the pinned backend after the "
              "swap -- cycle 1's final text went nowhere")
    else:
        print(f"    OK (P2a): cycle 1's post-swap ops {a_after_swap} stayed on "
              f"the pinned backend")
    ends = A.ends()
    if not ends:
        failures += 1
        print("    FAIL (P2b): the outgoing backend was never end()ed at the "
              "re-pin -- only cancel()ed, and it re-acquired on cycle 1's "
              "final commit")
    elif cycle2_at and not any(swap_at < t for t in ends):
        failures += 1
        print("    FAIL (P2b): the outgoing backend was not released after "
              "the swap")
    elif B.ops and not any(t < B.first_op_at() for t in ends):
        failures += 1
        print("    FAIL (P2b): the outgoing backend was not released before "
              "cycle 2 started typing through the new one")
    else:
        print("    OK (P2b): the outgoing backend was released at the re-pin")
    stranded = [be.name for be in (A, B) if be.stranded()]
    if stranded:
        failures += len(stranded)
        print(f"    FAIL (P2c): {stranded} left acquired with no end() after "
              f"the last op -- stranded until the 120 s watchdog")
    else:
        print("    OK (P2c): no backend left holding the input method")
    failures += check_release_ordering("P2d", A, B)
    return failures


def check_release_ordering(label, *backends):
    """#57/#72 interaction: the pinned backend is released in the post-loop
    cleanup, which runs AFTER step 9 delivers -- including on the latched
    dead-recorder path, which delivers through that same normal path and only
    then reports. Encoded so a future edit that hoists end() above the
    delivery (or swaps it for cancel(), which DISCARDS) fails loudly."""
    failures = 0
    for be in backends:
        if be.end_before_delivery():
            failures += 1
            print(f"    FAIL ({label}): {be.name} was end()ed mid-delivery -- "
                  f"a coalesced commit would be flushed half-finished")
        elif be.deliveries() and not be.released_after_delivery():
            failures += 1
            print(f"    FAIL ({label}): {be.name} delivered text but was never "
                  f"released afterwards")
    if not failures:
        delivered = [b.name for b in backends if b.deliveries()]
        print(f"    OK ({label}): every delivery on {delivered} precedes its "
              f"release; nothing flushed mid-utterance")
    return failures


def scenario_p3():
    failures = 0
    print("  P3: swapping to a backend with no `name` must not crash the cycle")
    A, partial, errors, ncycles = run_p3()
    crash = [e for e in errors if "STT failed" in e or "AttributeError" in e]
    print(f"    A(ydotool) ops={[o for o, _ in A.ops]}")
    print(f"    cycles={ncycles}; partial backend received {partial.text}")
    print(f"    Error signals: {errors}")
    if ncycles < 2:
        why = (f"the cycle died first: {crash}" if crash
               else "the loop never produced a second utterance")
        print(f"    FAIL (P3): never reached the re-pin, which is the line "
              f"under test -- {why}")
        return failures + 1
    if crash:
        failures += 1
        print(f"    FAIL (P3): the cycle died on the partial backend: {crash} "
              f"-- an unguarded attribute in a diagnostic must never take the "
              f"utterance down")
    elif not A.ops:
        failures += 1
        print("    FAIL (P3): nothing was typed at all before the swap")
    else:
        print("    OK (P3): the swap logged and released without crashing")
    return failures


SCENARIOS = {"p1": scenario_p1, "p2": scenario_p2, "p3": scenario_p3}


def main():
    # P1 and P2 must not share a process: gnome-speaks-service.py takes its
    # CONFIG from the speech-to-cli `state` module, which lives in sys.modules
    # -- so two harness.load()s hand back the SAME dict and the scenarios
    # would silently overwrite each other's mode flags. (Measured: the second
    # scenario ran with loop=False.)
    if len(sys.argv) > 1:
        return SCENARIOS[sys.argv[1]]()

    print(f"service under test: {harness.SVC_PATH}")
    print()
    failures = 0
    for name in ("p1", "p2", "p3"):
        env = dict(os.environ, GS_REPRO_SCRATCH=os.path.join(SCRATCH, name))
        proc = subprocess.run([sys.executable, os.path.abspath(__file__), name], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=180)
        sys.stdout.write(proc.stdout)
        if proc.returncode and not proc.stdout.strip():
            # The scenario died before it could assert anything -- usually a
            # bad GS_SVC_PATH (the harness puts dirname(SVC_PATH) on sys.path,
            # so the file needs its sibling injector.py/spellbook.py beside
            # it). Silence here reads as "no findings", which is worse than
            # noise.
            print(f"    ERROR: scenario {name} crashed before asserting:")
            for line in proc.stderr.strip().splitlines()[-4:]:
                print(f"      {line}")
        failures += proc.returncode
        print()
    if failures:
        print(f"FAIL: {failures} assertion(s) violated -- the injector pin is "
              f"not released when it changes / at cycle exit.")
        return 1
    print("PASS: the pin holds for the whole utterance AND is released both "
          "when it changes and at cycle exit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
