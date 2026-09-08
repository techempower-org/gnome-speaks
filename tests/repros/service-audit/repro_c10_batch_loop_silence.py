#!/usr/bin/env python3
"""repro: on the batch (vad) STT path, Continuous Dictation survives silence
(#166) -- the loop's semantics match the streaming cycle loop.

Streaming: a cycle with no speech is "no speech in loop cycle, continuing";
only a stop (#110/#112) or LOOP_ERROR_CAP consecutive error cycles (#117) end
the run. Batch, on the baseline (5dde4b4): _deliver_stt_result restarted only
under `if user_text and ...`, so a silent {"text": ""} -- what stt_vad returns
after NO_SPEECH_TIMEOUT (7 s) of quiet -- went idle with no restart and no
toast, Loop pill still on. start_listening() routes every session here while
wyoming.skip_azure() holds, i.e. ALWAYS with speech_backend=local, so "Loop"
meant one utterance or 7 s of quiet, then silently off.

  L1  silence x3 then text, loop on   -> the loop runs through the silences,
      types the text, and is STILL LISTENING afterwards (cycles >= 5, no
      Error).  Baseline: 1 cycle, nothing typed, idle.
  L2  a badge tap (the REAL stop_listening(), from another thread, as D-Bus
      would call it) during the 3rd cycle: the recorder sees it as stop_when
      (#110), that cycle's words are KEPT and typed, and the loop ENDS --
      exactly 3 cycles, idle, no Error, loop flag still on.
  L3  a panic stop() during a silent cycle -> the cycle's result is
      DISCARDED, idle, nothing typed, no restart (2 cycles).
  L4  silence then errors -> the #117 cap still ends the run: [q, e, e, e] =
      4 cycles, ONE route-naming toast, idle; and clean silence RESETS the
      streak ([e, e, q, e, e, e] = 6 cycles, one toast) -- c9 claimed that,
      but it was untestable while silence ended the loop.
  L5  {"text": "", "status": "NoAudio"} -- the recorder yielded no frames (a
      lost mic), which stt_vad answers in milliseconds -- is an ERROR cycle,
      not silence: loop on -> capped at 3 cycles with one toast naming the
      recorder (never a hot loop of recorder spawns); loop off -> one toast at
      once, one cycle.  Baseline: a silent idle, no toast, either way.
  L6  control: loop OFF, silence -> one cycle, idle, no restart, no Error
      (green on both sides).

Every fake silent cycle sleeps QUIET_SLEEP: GLib.idle_add is stubbed to call
through, so the restart is inline, and L1's verdict is a COUNT of cycles, not
a rate. A case that does not end by itself is ended by stop() after `window`.

Run:  GS_SVC_PATH=<gnome-speaks-service.py> python3 repro_c10_batch_loop_silence.py
Exit 0 = all hold; 1 = a verdict failed; 2 = setup failure.
"""
import sys
import threading
import time

import harness

FAILS = []
CAP = 3                 # the contract, not mod.LOOP_ERROR_CAP
ROUTE_WORDS = "Azure on cooldown after a failure"     # _SPEECH_ROUTE_WORDS["azure_down"]
SETTLE = 6.0            # s to wait for a run to go quiet
WINDOW = 1.5            # s a loop that is SUPPOSED to keep running gets before we look
QUIET_SLEEP = 0.05      # s a fake silent cycle takes
NO_AUDIO = {"text": "", "status": "NoAudio"}   # stt_vad(): recorder yielded zero frames


def check(label, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} ({label}) {detail}")
    if not cond:
        FAILS.append(label)


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

    def paste(self, text):
        self.typed.append(text)

    def finalize(self, text):
        self.typed.append(text)

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


# Script steps. Each is called with ctx = {"svc": service, "kw": dispatch kwargs}
# ON THE STT THREAD (so it may raise, and may see stop_when).
def quiet(ctx):
    time.sleep(QUIET_SLEEP)
    return {"text": ""}


def boom(ctx):
    raise RuntimeError("fake: Azure STT unreachable; offline fallback failed")


def text(t):
    return lambda ctx: {"text": t}


def wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def tap_then(t, seen):
    """A badge tap mid-cycle: stop_listening() from ANOTHER thread (it joins the
    STT thread, so it can never be called from the recorder's own thread), then
    the recorder finishes early with the words it has -- exactly what
    record_with_vad(stop_when=...) does (#110)."""
    def step(ctx):
        svc, kw = ctx["svc"], ctx["kw"]
        stop_when = kw.get("stop_when")
        seen["stop_when_passed"] = stop_when is not None
        threading.Thread(target=svc.stop_listening, daemon=True).start()
        seen["stop_when_fired"] = bool(stop_when) and wait_until(stop_when)
        return {"text": t}
    return step


def panic_stop_then(t, seen):
    """stop() from another thread mid-cycle; the library then returns a result
    the service must DISCARD (its token is cancelled)."""
    def step(ctx):
        svc = ctx["svc"]
        threading.Thread(target=svc.stop, daemon=True).start()
        seen["stop_event_set"] = wait_until(svc._stop_event.is_set)
        return {"text": t}
    return step


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
def fresh_service(mod, loop_on):
    svc = harness.make_service(mod)
    svc._stt_mode = "vad"
    svc._reload_config_flags = lambda *a, **k: None   # keep the pins below
    svc._save_config_flag = lambda *a, **k: None
    svc._wake_gate_blocks = lambda: False
    svc._try_cast = lambda text: False
    # _drain_speech_gap is deliberately NOT stubbed: the silence branch calls
    # it, and on an empty queue it must return at once.
    errors = []
    svc._emit_error = lambda msg: errors.append(msg) or False
    mod.CONFIG["continuous_dictation"] = loop_on
    mod.CONFIG["conversation_mode"] = False
    mod.CONFIG["dictation_mode"] = True
    return svc, errors


def settle(svc, quiet_s=0.3, timeout=SETTLE):
    """True once the STT thread is gone and state has been idle for quiet_s."""
    deadline = time.monotonic() + timeout
    idle_since = None
    while time.monotonic() < deadline:
        t = svc._stt_thread
        alive = t is not None and t.is_alive()
        if not alive and svc.current_state == "idle":
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since >= quiet_s:
                return True
        else:
            idle_since = None
        time.sleep(0.02)
    return False


def run_batch(mod, script, loop_on=True, window=SETTLE):
    calls = [0]
    inj = _Inj()
    mod.get_injector = lambda: inj
    mod.HAS_VAD = True
    mod.GLib.idle_add = lambda fn, *a, **k: fn(*a, **k)    # run restarts inline
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
    rc = svc.start_listening()
    if rc != "ok":
        raise RuntimeError(f"start_listening refused: {rc}")
    settled = settle(svc, timeout=window)
    states = set()
    for _ in range(10):                 # a still-running loop shows "listening"
        states.add(svc.current_state)
        time.sleep(0.02)
    if not settled:
        svc.stop()
        t = svc._stt_thread
        if t is not None:
            t.join(timeout=2.0)
    return dict(cycles=calls[0], errors=errors, typed=inj.typed, settled=settled,
                states=states, flag=mod.CONFIG.get("continuous_dictation"))


def is_cap_toast(msg):
    return ROUTE_WORDS in msg


def main():
    try:
        mod, _events = harness.load()
    except Exception as exc:
        print(f"!! SETUP FAILURE: cannot load service: {exc}")
        return 2
    print(f"service under test: {harness.SVC_PATH}")
    if not getattr(mod, "_STT_HAS_STOP_WHEN", False):
        print("!! SETUP FAILURE: speech-to-cli has no stop_when seam; L2 cannot mean anything")
        return 2
    mod.wyoming_mod.skip_reason = lambda: "azure_down"     # pin the toast literal
    if mod._speech_route_words() != ROUTE_WORDS:
        print(f"!! SETUP FAILURE: route pin did not take: {mod._speech_route_words()!r}")
        return 2

    # --- L1: silence continues the loop; text is typed; still listening -----
    r = run_batch(mod, [quiet, quiet, quiet, text("hello there")], window=WINDOW)
    check("L1", not r["settled"] and r["cycles"] >= 5 and r["typed"] == ["hello there"]
          and r["errors"] == [] and "listening" in r["states"] and r["flag"] is True,
          f"cycles={r['cycles']} typed={r['typed']!r} errors={r['errors']!r} "
          f"settled={r['settled']} states={sorted(r['states'])}")

    # --- L2: a badge tap ends the run and keeps the words (#110) -------------
    seen = {}
    r = run_batch(mod, [quiet, quiet, tap_then("kept words", seen)])
    check("L2", r["settled"] and r["cycles"] == 3 and r["typed"] == ["kept words"]
          and r["errors"] == [] and seen.get("stop_when_passed") and seen.get("stop_when_fired")
          and r["flag"] is True,
          f"cycles={r['cycles']} typed={r['typed']!r} errors={r['errors']!r} "
          f"settled={r['settled']} seen={seen}")

    # --- L3: a panic stop() discards and does not restart -------------------
    seen = {}
    r = run_batch(mod, [quiet, panic_stop_then("must be discarded", seen)])
    check("L3", r["settled"] and r["cycles"] == 2 and r["typed"] == [] and r["errors"] == []
          and seen.get("stop_event_set"),
          f"cycles={r['cycles']} typed={r['typed']!r} errors={r['errors']!r} "
          f"settled={r['settled']} seen={seen}")

    # --- L4: the error cap still ends the run; silence resets the streak -----
    r = run_batch(mod, [quiet, boom, boom, boom])
    caps = [e for e in r["errors"] if is_cap_toast(e)]
    check("L4a", r["settled"] and r["cycles"] == 1 + CAP and len(r["errors"]) == 1
          and len(caps) == 1 and r["typed"] == [] and r["flag"] is True,
          f"cycles={r['cycles']} errors={r['errors']!r} settled={r['settled']}")
    r = run_batch(mod, [boom, boom, quiet, boom, boom, boom])
    caps = [e for e in r["errors"] if is_cap_toast(e)]
    check("L4b", r["settled"] and r["cycles"] == 6 and len(r["errors"]) == 1
          and len(caps) == 1 and r["typed"] == [],
          f"cycles={r['cycles']} errors={r['errors']!r} settled={r['settled']}")

    # --- L5: NoAudio is an error cycle, not silence --------------------------
    r = run_batch(mod, [NO_AUDIO] * 12)
    caps = [e for e in r["errors"] if is_cap_toast(e)]
    check("L5a", r["settled"] and r["cycles"] == CAP and len(r["errors"]) == 1
          and len(caps) == 1 and "no audio" in r["errors"][0] and r["flag"] is True,
          f"cycles={r['cycles']} errors={r['errors']!r} settled={r['settled']}")
    r = run_batch(mod, [NO_AUDIO] * 3, loop_on=False)
    check("L5b", r["settled"] and r["cycles"] == 1 and len(r["errors"]) == 1
          and "no audio" in r["errors"][0] and not is_cap_toast(r["errors"][0]),
          f"cycles={r['cycles']} errors={r['errors']!r} settled={r['settled']}")

    # --- L6: control -- loop OFF, silence is one cycle and idle --------------
    r = run_batch(mod, [quiet], loop_on=False)
    check("L6", r["settled"] and r["cycles"] == 1 and r["errors"] == [] and r["typed"] == [],
          f"cycles={r['cycles']} errors={r['errors']!r} settled={r['settled']}")

    if FAILS:
        print(f"FAIL: {len(FAILS)} verdict(s) -- batch-path continuous dictation does not "
              f"survive silence the way the streaming loop does: {FAILS}")
        return 1
    print("PASS: batch-path loop continues through silence, a tap/stop ends it, the error cap "
          "still applies, and NoAudio is an error cycle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
