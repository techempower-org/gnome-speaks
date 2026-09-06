#!/usr/bin/env python3
"""NEGATIVE CONTROL: a live recorder must never be reported as a lost one.

Every other repro here asserts that a death IS seen.  This one asserts the
opposite direction, because a liveness detector that fails toward a false
positive would end a perfectly good loop session on the first quiet cycle and
tell the user their microphone was unplugged.  Healthy recorder, loop mode,
several cycles: no microphone Error, and the loop keeps looping.

exit 0 = clean, exit 1 = the detector fires on a live mic.
"""
import sys
import time

import fakes
import harness

WINDOW = 4.0
MIN_CYCLES = 3


def main():
    mod, _events, inj = harness.load()
    print(f"service under test: {harness.SVC_PATH}")

    cycles = [0]
    errors = []

    mod.HAS_WS = True
    mod.HAS_VAD = False
    mod.CONFIG["continuous_dictation"] = True       # LOOP
    mod.CONFIG["conversation_mode"] = False
    mod._take_prewarmed_rec = lambda *a, **k: fakes.LiveProc()
    mod._get_stt_ws = lambda *a, **k: (
        fakes.LoopingWS(mod.websocket.WebSocketTimeoutException,
                        script=[b"turn_end"], delay=0.05), True)
    mod._make_ws_audio_msg = lambda *a, **k: b""
    mod._rest_stt_fallback = lambda *a, **k: ""
    mod._invalidate_stt_ws = lambda *a, **k: None
    mod._parse_ws_msg = lambda msg, *a, **k: "turn_end"

    def count_cycle(*a, **k):
        cycles[0] += 1
    mod._init_stt_ws_session = count_cycle

    mod.GLib.idle_add = lambda fn, *a, **k: fn(*a, **k)

    svc = harness.make_service(mod)
    svc._stt_mode = "streaming"
    svc._reload_config_flags = lambda: None
    svc._emit_error = lambda msg: errors.append(msg) or False
    svc._try_cast = lambda text: False
    svc._drain_speech_gap = lambda *a, **k: None

    assert svc.start_listening() == "ok", "setup: start_listening refused"
    time.sleep(WINDOW)
    state_seen = svc.current_state
    cycles_seen = cycles[0]
    svc.stop()
    time.sleep(0.5)

    failures = 0
    print(f"  cycles run in {WINDOW:.0f}s: {cycles_seen}")
    print(f"  Error signals: {errors}")
    print(f"  state during the run: {state_seen!r}")
    if any("icrophone" in e for e in errors):
        failures += 1
        print("    FAIL: a live recorder was reported as a lost microphone")
    if cycles_seen < MIN_CYCLES:
        failures += 1
        print(f"    FAIL: loop stalled ({cycles_seen} < {MIN_CYCLES} cycles)")
    if state_seen != "listening":
        failures += 1
        print(f"    FAIL: state was {state_seen!r} mid-loop, expected 'listening'")
    print()
    if failures:
        print(f"FAIL: {failures} -- the detector fires on a healthy recorder")
        return 1
    print("PASS: healthy loop keeps looping, nothing reported")
    return 0


if __name__ == "__main__":
    sys.exit(main())
