#!/usr/bin/env python3
"""#57 / reviewer finding 1: single-shot never reports the lost mic.

The mic is yanked during a single-shot dictation.  The sender hits EOF, sends
the end-of-audio marker, and Azure answers turn.end -- which in single-shot
mode sets _stop_event.  Any dead-recorder verdict gated on `not _stopping()`
is therefore defeated by the session's OWN natural end, and the user is told
nothing at all.

Expected: exactly one microphone Error, one cycle, idle, no restart.
exit 0 = clean, exit 1 = bug present.
"""
import sys
import time

import fakes
import harness

WINDOW = 6.0


def main():
    mod, _events, inj = harness.load()
    print(f"service under test: {harness.SVC_PATH}")

    cycles = [0]
    errors = []
    restarts = []

    mod.HAS_WS = True
    mod.HAS_VAD = False
    mod.CONFIG["continuous_dictation"] = False      # SINGLE SHOT
    mod.CONFIG["conversation_mode"] = False
    mod._take_prewarmed_rec = lambda *a, **k: fakes.DeadProc()
    mod._get_stt_ws = lambda *a, **k: (
        fakes.ScriptedWS(mod.websocket.WebSocketTimeoutException,
                         script=[b"turn_end"], delay=0.2), True)
    mod._make_ws_audio_msg = lambda *a, **k: b""
    mod._rest_stt_fallback = lambda *a, **k: ""
    mod._invalidate_stt_ws = lambda *a, **k: None

    def fake_parse(msg, phrases, partial_holder, *a, **k):
        return "turn_end" if msg == b"turn_end" else None
    mod._parse_ws_msg = fake_parse

    def count_cycle(*a, **k):
        cycles[0] += 1
    mod._init_stt_ws_session = count_cycle

    mod.GLib.idle_add = lambda fn, *a, **k: fn(*a, **k)

    svc = harness.make_service(mod)
    svc._stt_mode = "streaming"
    svc._reload_config_flags = lambda: None
    svc._emit_error = lambda msg: errors.append(msg) or False
    svc._try_cast = lambda text: False
    _real_start = svc.start_listening
    svc.start_listening = lambda *a, **k: (restarts.append(time.monotonic()),
                                           _real_start(*a, **k))[-1]

    assert svc.start_listening() == "ok", "setup: start_listening refused"
    restarts.clear()                      # the priming call is not a restart
    idle_at = [None]
    t0 = time.monotonic()
    if harness.wait_for(lambda: svc.current_state == "idle", WINDOW):
        idle_at[0] = time.monotonic() - t0
    time.sleep(0.4)                       # give a rogue restart room to show
    state_seen = svc.current_state
    svc.stop()
    time.sleep(0.3)

    failures = 0
    print(f"  cycles: {cycles[0]}")
    print(f"  Error signals: {errors}")
    print(f"  restarts after the session: {len(restarts)}")
    print(f"  state: {state_seen!r}"
          + (f" (idle after {idle_at[0]:.1f}s)" if idle_at[0] is not None else ""))
    if len([e for e in errors if "icrophone" in e]) != 1:
        failures += 1
        print("    FAIL: expected exactly one microphone Error signal")
    if cycles[0] != 1:
        failures += 1
        print("    FAIL: single-shot should run exactly one cycle")
    if idle_at[0] is None:
        failures += 1
        print("    FAIL: service did not return to idle")
    if restarts:
        failures += 1
        print("    FAIL: restarted listening onto a dead recorder")
    leaked = svc._cancels.live()
    if leaked:
        failures += 1
        print(f"    FAIL: leaked cancel token(s) {leaked}")
    print()
    if failures:
        print(f"FAIL: {failures} -- turn.end masks the lost microphone")
        return 1
    print("PASS: single-shot + turn.end still reports the lost microphone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
