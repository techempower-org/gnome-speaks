#!/usr/bin/env python3
"""repro: in Continuous Dictation, STT ERRORS stop the loop after a cap; silence
never does (#117, a #110 follow-up).

Two loop shapes, two different failures on the pre-fix baseline (a20afea):

  streaming  A WebSocket that dies INSIDE a cycle (connect + session init
             succeed, recv/send raise) produced no phrases, and step 9 read
             "no phrases" as "no speech in loop cycle, continuing" -- so the
             cycle loop re-entered listening FOREVER, silently: no toast, no
             idle, one reconnect per cycle.  This is the loop the issue names.
  batch      A raising stt_dispatch (or the #130 {"error": ...} dict) toasted
             on the FIRST error and ended the run -- no retry at all, and the
             toast never named the speech route.  Not a spin, but one Azure
             hiccup killed the loop.

After the fix both paths agree: 3 CONSECUTIVE error cycles stop the loop for
this run, go idle, emit exactly ONE Error signal that names the route
(_speech_route_words()), and leave CONFIG['continuous_dictation'] on so the
next tap resumes the loop.  A cycle that produces text -- or clean silence --
resets the streak.

  B1  batch, stt_dispatch RAISES every cycle          -> 3 cycles, 1 Error
      (route words + the exception), idle, flag still True
  B2  batch, stt_dispatch returns {"error": ...}     -> same (the #130 shape)
  B3  batch, errors are NOT consecutive (e e text e e e) -> the text resets the
      streak: 6 cycles, 1 Error, and the text was typed
  B4  control: batch SILENCE ({"text": ""}) never trips the cap -> 0 Errors
  B5  control: loop OFF, one error -> exactly 1 Error at once (#130 preserved),
      1 cycle, and it does not carry the loop wording
  S1  streaming, ws.recv/ws.send raise every cycle   -> 3 cycles, 1 Error
      (route words), idle, flag still True.  On the baseline this ran ~dozens
      of cycles in the window with ZERO Errors.
  S2  control: streaming SILENCE (turn_end, no phrases) for several cycles
      -> 0 Errors, still listening (repro_i's loop, plus "the cap stays quiet")

The route is PINNED to "azure_down" (wyoming.skip_reason) so the assertion is
on a literal ("Azure on cooldown after a failure"), not on whatever the
function under test happens to return.

Run:  GS_SVC_PATH=<gnome-speaks-service.py> python3 repro_c9_loop_error_cap.py
Exit 0 = all hold; 1 = a verdict failed; 2 = setup failure.
"""
import sys
import time

import harness

FAILS = []
CAP = 3                 # the contract, not mod.LOOP_ERROR_CAP: a changed cap must fail here
ROUTE_WORDS = "Azure on cooldown after a failure"     # _SPEECH_ROUTE_WORDS["azure_down"]
SETTLE = 6.0            # s to wait for a batch run to go quiet
STREAM_WINDOW = 4.0     # s the streaming session may run before we look


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


class LiveStdout:
    def read(self, n):
        time.sleep(0.005)
        return b"\x00" * n


class LiveProc:
    """A healthy recorder: the dead-recorder verdict must stay out of this."""
    returncode = None

    def __init__(self):
        self.stdout = LiveStdout()

    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


class DyingWS:
    """Connect and session init succeed; the socket then dies under us."""

    def settimeout(self, t):
        pass

    def send(self, payload, opcode=None):
        raise ConnectionResetError("fake: connection reset by peer")

    def recv(self):
        time.sleep(0.02)
        raise ConnectionResetError("fake: connection reset by peer")


class SilentWS:
    """Azure answers every utterance with a bare turn_end: clean silence."""

    def settimeout(self, t):
        pass

    def send(self, payload, opcode=None):
        pass

    def recv(self):
        time.sleep(0.05)
        return b"turn_end"


# ---------------------------------------------------------------------------
# drivers
# ---------------------------------------------------------------------------
def fresh_service(mod, loop_on, stt_mode):
    svc = harness.make_service(mod)
    svc._stt_mode = stt_mode
    svc._reload_config_flags = lambda *a, **k: None   # keep the pins below
    svc._save_config_flag = lambda *a, **k: None
    svc._wake_gate_blocks = lambda: False
    svc._try_cast = lambda text: False
    svc._drain_speech_gap = lambda *a, **k: None
    errors = []
    svc._emit_error = lambda msg: errors.append(msg) or False
    mod.CONFIG["continuous_dictation"] = loop_on
    mod.CONFIG["conversation_mode"] = False
    mod.CONFIG["dictation_mode"] = True
    return svc, errors


def settle(svc, quiet=0.3, timeout=SETTLE):
    """Wait until the STT thread is gone and state has been idle for `quiet` s."""
    deadline = time.monotonic() + timeout
    idle_since = None
    while time.monotonic() < deadline:
        t = svc._stt_thread
        alive = t is not None and t.is_alive()
        if not alive and svc.current_state == "idle":
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since >= quiet:
                return True
        else:
            idle_since = None
        time.sleep(0.02)
    return False


def run_batch(mod, script, loop_on=True):
    """Drive the batch (vad) worker with a scripted stt_dispatch.

    script: list of callables or values; each cycle pops one.  A callable is
    CALLED (so it may raise); anything else is returned as the result.  When the
    script runs out the fake returns silence, which ends a batch loop cleanly.
    Returns (cycles, errors, typed, settled, flag_after).
    """
    calls = [0]
    inj = _Inj()
    mod.get_injector = lambda: inj
    mod.HAS_VAD = True
    mod.GLib.idle_add = lambda fn, *a, **k: fn(*a, **k)    # run restarts inline

    def fake_dispatch(**kw):
        i = calls[0]
        calls[0] += 1
        if i >= len(script):
            return {"text": ""}
        step = script[i]
        return step() if callable(step) else step
    mod.stt_dispatch = fake_dispatch

    svc, errors = fresh_service(mod, loop_on, "vad")
    rc = svc.start_listening()
    if rc != "ok":
        raise RuntimeError(f"start_listening refused: {rc}")
    settled = settle(svc)
    if not settled:
        svc.stop()
        time.sleep(0.3)
    return calls[0], errors, inj.typed, settled, mod.CONFIG.get("continuous_dictation")


def run_streaming(mod, ws, window=STREAM_WINDOW):
    """Drive the streaming cycle with a fake recorder and a fake WebSocket."""
    cycles = [0]
    inj = _Inj()
    mod.get_injector = lambda: inj
    mod.HAS_WS = True
    mod.HAS_VAD = False
    mod.GLib.idle_add = lambda fn, *a, **k: fn(*a, **k)
    mod._take_prewarmed_rec = lambda *a, **k: LiveProc()
    mod._get_stt_ws = lambda *a, **k: (ws, True)
    mod._make_ws_audio_msg = lambda *a, **k: b""
    mod._rest_stt_fallback = lambda *a, **k: ""
    mod._invalidate_stt_ws = lambda *a, **k: None
    mod._parse_ws_msg = lambda msg, *a, **k: "turn_end"
    mod.wyoming_mod.skip_azure = lambda: False       # streaming stays streaming

    def count_cycle(*a, **k):
        cycles[0] += 1
    mod._init_stt_ws_session = count_cycle

    svc, errors = fresh_service(mod, True, "streaming")
    rc = svc.start_listening()
    if rc != "ok":
        raise RuntimeError(f"start_listening refused: {rc}")
    # Either the cap ends the session (settle returns early) or the window does.
    settled = settle(svc, quiet=0.3, timeout=window)
    state_seen = svc.current_state
    if not settled:
        svc.stop()
        time.sleep(0.3)
    return cycles[0], errors, settled, state_seen, mod.CONFIG.get("continuous_dictation")


def is_cap_toast(msg):
    return ROUTE_WORDS in msg


def main():
    try:
        mod, _events = harness.load()
    except Exception as exc:
        print(f"!! SETUP FAILURE: cannot load service: {exc}")
        return 2
    print(f"service under test: {harness.SVC_PATH}")
    # Pin the route so the toast assertion is on a literal.
    mod.wyoming_mod.skip_reason = lambda: "azure_down"
    if mod._speech_route_words() != ROUTE_WORDS:
        print(f"!! SETUP FAILURE: route pin did not take: {mod._speech_route_words()!r}")
        return 2

    def boom():
        raise RuntimeError("fake: Azure STT unreachable; offline fallback failed")

    # --- B1: batch, raising every cycle ------------------------------------
    cycles, errors, typed, settled, flag = run_batch(mod, [boom] * 12)
    caps = [e for e in errors if is_cap_toast(e)]
    check("B1", settled and cycles == CAP and len(errors) == 1 and len(caps) == 1
          and "fake: Azure STT unreachable" in errors[0] and flag is True and typed == [],
          f"cycles={cycles} errors={errors!r} settled={settled} flag={flag}")

    # --- B2: batch, the #130 error dict every cycle -----------------------
    err = {"error": "Azure STT unreachable (ConnectionError); offline fallback failed"}
    cycles, errors, typed, settled, flag = run_batch(mod, [err] * 12)
    caps = [e for e in errors if is_cap_toast(e)]
    check("B2", settled and cycles == CAP and len(errors) == 1 and len(caps) == 1
          and "ConnectionError" in errors[0] and flag is True and typed == [],
          f"cycles={cycles} errors={errors!r} settled={settled} flag={flag}")

    # --- B3: consecutive means consecutive ---------------------------------
    script = [boom, boom, {"text": "hello there"}, boom, boom, boom, boom, boom]
    cycles, errors, typed, settled, flag = run_batch(mod, script)
    check("B3", settled and cycles == 6 and len(errors) == 1 and is_cap_toast(errors[0])
          and typed == ["hello there"] and flag is True,
          f"cycles={cycles} errors={len(errors)} typed={typed!r} settled={settled}")

    # --- B4: control -- silence never trips the cap -------------------------
    cycles, errors, typed, settled, flag = run_batch(mod, [{"text": ""}] * 6)
    check("B4", settled and errors == [] and flag is True and typed == [],
          f"cycles={cycles} errors={errors!r} settled={settled} flag={flag}")

    # --- B5: control -- loop OFF keeps the #130 immediate toast -------------
    cycles, errors, typed, settled, flag = run_batch(mod, [boom] * 3, loop_on=False)
    check("B5", settled and cycles == 1 and len(errors) == 1
          and "fake: Azure STT unreachable" in errors[0] and not is_cap_toast(errors[0]),
          f"cycles={cycles} errors={errors!r}")

    # --- S1: streaming, the socket dies inside every cycle ------------------
    cycles, errors, settled, state_seen, flag = run_streaming(mod, DyingWS())
    caps = [e for e in errors if is_cap_toast(e)]
    check("S1", settled and cycles == CAP and len(errors) == 1 and len(caps) == 1
          and "connection reset" in errors[0] and state_seen == "idle" and flag is True,
          f"cycles={cycles} errors={errors!r} settled={settled} state={state_seen} flag={flag}")

    # --- S2: control -- streaming silence keeps looping, no toast -----------
    cycles, errors, settled, state_seen, flag = run_streaming(mod, SilentWS(), window=2.0)
    check("S2", not settled and cycles >= 3 and errors == [] and state_seen == "listening",
          f"cycles={cycles} errors={errors!r} state={state_seen}")

    if FAILS:
        print(f"FAIL: {len(FAILS)} verdict(s) -- loop-mode STT errors are not capped at "
              f"{CAP} consecutive cycles with one route-naming toast: {FAILS}")
        return 1
    print(f"PASS: {CAP} consecutive STT error cycles stop the loop with one route-naming "
          "toast; text and silence reset it; the loop flag survives")
    return 0


if __name__ == "__main__":
    sys.exit(main())
