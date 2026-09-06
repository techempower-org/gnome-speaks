#!/usr/bin/env python3
"""#57 / reviewer finding 2: the dead-recorder branch throws away the words.

The user dictates "hello world"; Azure returns the phrase; THEN the mic is
yanked.  A dead-recorder branch that fires before the transcript is assembled
discards `phrases` and backspaces the live-typed partial, so speech that was
already recognized never reaches the cursor.  Reporting the fault must be
ADDITIONAL to delivering the text, never instead of it.

Expected: "hello world" reaches the cursor AND exactly one microphone Error.
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

    mod.HAS_WS = True
    mod.HAS_VAD = False
    mod.CONFIG["continuous_dictation"] = False
    mod.CONFIG["conversation_mode"] = False
    mod.CONFIG["dictation_mode"] = True
    mod._take_prewarmed_rec = lambda *a, **k: fakes.DeadProc()
    mod._get_stt_ws = lambda *a, **k: (
        fakes.ScriptedWS(mod.websocket.WebSocketTimeoutException,
                         script=[b"phrase"], delay=0.15), True)
    mod._make_ws_audio_msg = lambda *a, **k: b""
    mod._rest_stt_fallback = lambda *a, **k: ""
    mod._invalidate_stt_ws = lambda *a, **k: None

    served = []

    def fake_parse(msg, phrases, partial_holder, *a, **k):
        if msg == b"phrase" and not served:
            served.append(True)
            phrases.append("hello world")
            partial_holder[0] = "hello world"
            return "phrase"
        return None
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
    svc._wake_gate_blocks = lambda: False

    assert svc.start_listening() == "ok", "setup: start_listening refused"
    harness.wait_for(lambda: svc.current_state == "idle", WINDOW)
    time.sleep(0.3)
    svc.stop()
    time.sleep(0.3)

    delivered = [t for t, _ts in inj.all_text()]
    failures = 0
    print(f"  cycles: {cycles[0]}")
    print(f"  text delivered to the cursor: {delivered}")
    print(f"  Error signals: {errors}")
    print(f"  backspaces: {inj.backspaces}")
    if not any("world" in t.lower() for t in delivered):
        failures += 1
        print("    FAIL: speech recognized before the yank was thrown away")
    if len([e for e in errors if "icrophone" in e]) != 1:
        failures += 1
        print("    FAIL: expected exactly one microphone Error signal")
    if svc.current_state != "idle":
        failures += 1
        print(f"    FAIL: state is {svc.current_state!r}, expected idle")
    leaked = svc._cancels.live()
    if leaked:
        failures += 1
        print(f"    FAIL: leaked cancel token(s) {leaked}")
    print()
    if failures:
        print(f"FAIL: {failures} -- the dead recorder cost the user their words")
        return 1
    print("PASS: text delivered, then the lost microphone reported once")
    return 0


if __name__ == "__main__":
    sys.exit(main())
