#!/usr/bin/env python3
"""repro: in loop mode the TEXT-branch restart must survive losing the idle
gap to the speech queue (#173).

After a batch (vad) cycle types its transcript, _deliver_stt_result goes
_idle_after_stt() -> idle -> GLib.idle_add(restart). The speech-queue
dispatcher may claim that idle gap for an agent item (an atomic
idle|speaking -> speaking claim since #168), and then start_listening(quick=True)
answers "error: busy (speaking)". On the baseline (683b0be) the restart callback
dropped that answer on the floor: the loop ended, no toast, no retry, Loop pill
still on. The silence branch never had this gap -- it calls
_drain_speech_gap() first (#171) -- so the two branches of the same loop
disagreed about whether an agent message ends a dictation run.

The gap is opened deterministically: GLib.idle_add is replaced by a PUMP whose
gate the repro closes while the cycle finishes, so the restart callback sits
queued exactly the way it does between the STT thread's idle and the main
loop's next iteration, and an agent item is enqueued and claimed in between.

  G1  text, then the queue claims the gap, then the item finishes -> the loop
      is LISTENING again (cycles >= 2, text typed, no toast). Baseline: idle,
      1 cycle, nothing said.
  G2  control: a panic stop() during the re-arm wait ends the run -- no
      restart after the item, no toast (green on both sides).
  G3  the #117 error cap still applies AFTER a re-armed restart:
      [text, e, e, e] with the gap claimed after the text = 4 cycles, ONE
      route-naming toast, idle. Baseline: 1 cycle, no toast.
  G4  the retry is BOUNDED: a queue that never goes quiet -> after
      LOOP_RESTART_RETRIES re-arms, ONE toast saying the loop stopped (through
      the #117 reporting path), idle, loop flag left on, never a hot retry.
      Baseline: silent, no toast.
  G5  control: loop OFF, text, the queue claims the gap -> one cycle, no
      restart, no toast (green on both sides).

Run:  GS_SVC_PATH=<gnome-speaks-service.py> python3 repro_c11_loop_gap_busy.py
Exit 0 = all hold; 1 = a verdict failed; 2 = setup failure.
"""
import collections
import sys
import threading
import time
import traceback

import harness
import isolation

FAILS = []
CAP = 3                 # the contract, not mod.LOOP_ERROR_CAP
ROUTE_WORDS = "Azure on cooldown after a failure"     # _SPEECH_ROUTE_WORDS["azure_down"]
SETTLE = 6.0
ITEM_SECONDS = 0.6      # how long one fake agent item "plays"


def check(label, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} ({label}) {detail}")
    if not cond:
        FAILS.append(label)


def wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return False


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class _Inj:
    name = "fake"

    def __init__(self):
        self.typed = []

    def available(self):
        return True

    def commit(self, text):
        self.typed.append(text)

    paste = finalize = commit

    def type_text(self, text):
        pass

    def send_backspaces(self, n):
        pass

    def supports_preedit(self):
        return False

    def end(self):
        pass

    def cancel(self):
        pass


class Pump:
    """A stand-in for the GLib main loop's idle sources: callbacks queue up and
    run on ONE thread, in order, only while the gate is open. Closing the gate
    is the repro's handle on the window between the STT thread's
    _set_state('idle') and the main loop running the restart callback."""

    def __init__(self, mod):
        self.q = collections.deque()
        self.gate = threading.Event()
        self.gate.set()
        self.ran = 0
        mod.GLib.idle_add = self.add
        threading.Thread(target=self._run, daemon=True, name="fake-main-loop").start()

    def add(self, fn, *a, **k):
        self.q.append((fn, a, k))
        return 1

    def _run(self):
        while True:
            self.gate.wait()
            try:
                fn, a, k = self.q.popleft()
            except IndexError:
                time.sleep(0.002)
                continue
            try:
                fn(*a, **k)
            except Exception:
                traceback.print_exc()
            self.ran += 1

    def drained(self):
        return not self.q


class FakeTTS:
    def __init__(self, mod):
        self.mod = mod
        self.seconds = ITEM_SECONDS
        self.played = []
        isolation.install_fake_tts(mod, self)

    def __call__(self, text, **kw):
        self.played.append(text)
        deadline = time.monotonic() + self.seconds
        while time.monotonic() < deadline:
            if self.mod.state._cancel_event.is_set():
                return {"spoken": False, "cancelled": True}
            time.sleep(0.01)
        return {"spoken": True}


def quiet(ctx):
    time.sleep(0.05)
    return {"text": ""}


def boom(ctx):
    raise RuntimeError("fake: Azure STT unreachable; offline fallback failed")


def text(t):
    return lambda ctx: {"text": t}


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
def fresh_service(mod, loop_on):
    svc = harness.make_service(mod)
    svc._stt_mode = "vad"
    svc._reload_config_flags = lambda *a, **k: None
    svc._save_config_flag = lambda *a, **k: None
    svc._wake_gate_blocks = lambda: False
    svc._try_cast = lambda text: False
    errors = []
    svc._emit_error = lambda msg: errors.append(msg) or False
    mod.CONFIG["continuous_dictation"] = loop_on
    mod.CONFIG["conversation_mode"] = False
    mod.CONFIG["dictation_mode"] = True
    return svc, errors


def queue_quiet(svc):
    with svc._queue_current_lock:
        busy = svc._queue_current is not None
    return not busy and svc._tts_queue.empty()


def stt_dead(svc):
    t = svc._stt_thread
    return t is None or not t.is_alive()


def settle(svc, pump, quiet_s=0.3, timeout=SETTLE):
    """True once the STT thread is gone, the pump is drained and state has
    been idle for quiet_s."""
    deadline = time.monotonic() + timeout
    idle_since = None
    while time.monotonic() < deadline:
        if stt_dead(svc) and svc.current_state == "idle" and pump.drained():
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since >= quiet_s:
                return True
        else:
            idle_since = None
        time.sleep(0.02)
    return False


def run_gap(mod, pump, tts, script, loop_on=True, items=1, during_item=None,
            expect_alive=True):
    """One text cycle whose idle gap the speech queue claims.

    Returns the measurements; the caller judges them."""
    calls = [0]
    inj = _Inj()
    mod.get_injector = lambda: inj
    mod.HAS_VAD = True
    holder = {}

    def fake_dispatch(**kw):
        i = calls[0]
        calls[0] += 1
        ctx = {"svc": holder["svc"], "kw": kw}
        if i >= len(script):
            return quiet(ctx)
        step = script[i]
        return step(ctx) if callable(step) else step
    mod.stt_dispatch = fake_dispatch

    svc, errors = fresh_service(mod, loop_on)
    holder["svc"] = svc
    seen = {}
    pump.gate.clear()                     # the main loop "has not run yet"
    try:
        rc = svc.start_listening()        # direct call, as D-Bus would
        if rc != "ok":
            raise RuntimeError(f"start_listening refused: {rc}")
        # The cycle types its text and drops to idle; its restart callback is
        # now QUEUED behind the closed gate.
        seen["idle_after_text"] = wait_until(
            lambda: svc.current_state == "idle" and stt_dead(svc) and inj.typed, 3.0)
        # An agent item lands in that gap and the dispatcher claims it.
        for i in range(items):
            svc.enqueue_speech(f"A{i} agent narrating", source="agent-x")
        seen["claimed"] = wait_until(lambda: svc.current_state == "speaking", 1.0)
        seen["restart_queued"] = not pump.drained()
    finally:
        pump.gate.set()                   # the main loop runs: restart fires now
    if during_item is not None:
        during_item(svc)
    # Let the queue play out, then give a re-armed restart time to land.
    seen["queue_quiet"] = wait_until(lambda: queue_quiet(svc) and svc.current_state != "speaking",
                                     items * tts.seconds + 3.0)
    if expect_alive:
        wait_until(lambda: svc.current_state == "listening" and calls[0] >= 2, 2.0)
    else:
        seen["settled"] = settle(svc, pump)
    states = set()
    for _ in range(10):
        states.add(svc.current_state)
        time.sleep(0.02)
    wait_until(pump.drained, 1.0)
    # Tear down whatever is still running so the next case starts clean.
    svc.stop()
    t = svc._stt_thread
    if t is not None:
        t.join(timeout=2.0)
    wait_until(pump.drained, 1.0)
    return dict(cycles=calls[0], errors=list(errors), typed=inj.typed, states=states,
                seen=seen, flag=mod.CONFIG.get("continuous_dictation"),
                played=list(tts.played))


def is_cap_toast(msg):
    return ROUTE_WORDS in msg


def main():
    try:
        mod, _events = harness.load()
    except Exception as exc:
        print(f"!! SETUP FAILURE: cannot load service: {exc}")
        return 2
    print(f"service under test: {harness.SVC_PATH}")
    mod.wyoming_mod.skip_reason = lambda: "azure_down"     # pin the toast literal
    if mod._speech_route_words() != ROUTE_WORDS:
        print(f"!! SETUP FAILURE: route pin did not take: {mod._speech_route_words()!r}")
        return 2
    pump = Pump(mod)
    tts = FakeTTS(mod)
    # The retry bound under test (G4). Absent on the baseline: the constant is
    # then just an unread attribute, and G4 goes red on the missing toast.
    mod.LOOP_RESTART_WAIT_SECONDS = 0.3

    # --- G1: the loop survives losing the gap ---------------------------------
    tts.played.clear()
    r = run_gap(mod, pump, tts, [text("hello there")])
    s = r["seen"]
    gap_ok = s.get("idle_after_text") and s.get("claimed") and s.get("restart_queued")
    if not gap_ok:
        print(f"!! SETUP FAILURE: the gap was not opened: {s}")
        return 2
    check("G1", "listening" in r["states"] and r["cycles"] >= 2
          and r["typed"] == ["hello there"] and r["errors"] == [] and r["flag"] is True
          and len(r["played"]) == 1,
          f"cycles={r['cycles']} typed={r['typed']!r} errors={r['errors']!r} "
          f"states={sorted(r['states'])} played={len(r['played'])} seen={s}")

    # --- G2: control -- stop() during the wait ends the run -------------------
    tts.played.clear()
    r = run_gap(mod, pump, tts, [text("then stopped")], during_item=lambda svc: svc.stop(),
                expect_alive=False)
    check("G2", r["seen"].get("settled") and r["cycles"] == 1 and r["errors"] == []
          and "listening" not in r["states"] and r["typed"] == ["then stopped"],
          f"cycles={r['cycles']} errors={r['errors']!r} states={sorted(r['states'])} "
          f"seen={r['seen']}")

    # --- G3: the error cap still applies after the re-armed restart -----------
    tts.played.clear()
    r = run_gap(mod, pump, tts, [text("one"), boom, boom, boom], expect_alive=False)
    caps = [e for e in r["errors"] if is_cap_toast(e)]
    check("G3", r["seen"].get("settled") and r["cycles"] == 1 + CAP and len(r["errors"]) == 1
          and len(caps) == 1 and r["typed"] == ["one"] and r["flag"] is True,
          f"cycles={r['cycles']} errors={r['errors']!r} seen={r['seen']}")

    # --- G4: bounded -- a queue that never goes quiet ends in ONE toast ------
    tts.played.clear()
    tts.seconds = 0.3
    r = run_gap(mod, pump, tts, [text("held out")], items=8, expect_alive=False)
    tts.seconds = ITEM_SECONDS
    stopped = [e for e in r["errors"] if "paused" in e.lower() and not is_cap_toast(e)]
    check("G4", r["seen"].get("settled") and r["cycles"] == 1 and len(r["errors"]) == 1
          and len(stopped) == 1 and "listening" not in r["states"] and r["flag"] is True
          and len(r["played"]) == 8,
          f"cycles={r['cycles']} errors={r['errors']!r} played={len(r['played'])} "
          f"states={sorted(r['states'])} seen={r['seen']}")

    # --- G5: control -- loop OFF, one cycle, no restart, no toast -------------
    tts.played.clear()
    r = run_gap(mod, pump, tts, [text("solo")], loop_on=False, expect_alive=False)
    check("G5", r["seen"].get("settled") and r["cycles"] == 1 and r["errors"] == []
          and r["typed"] == ["solo"] and "listening" not in r["states"],
          f"cycles={r['cycles']} errors={r['errors']!r} states={sorted(r['states'])} "
          f"seen={r['seen']}")

    if FAILS:
        print(f"FAIL: {len(FAILS)} verdict(s) -- the loop's text-branch restart does not "
              f"survive losing the idle gap to the speech queue: {FAILS}")
        return 1
    print("PASS: a loop restart that loses the idle gap to the speech queue re-arms after "
          "the item, a stop still ends it, the error cap still applies, and the retry is "
          "bounded with one toast")
    return 0


if __name__ == "__main__":
    sys.exit(main())
