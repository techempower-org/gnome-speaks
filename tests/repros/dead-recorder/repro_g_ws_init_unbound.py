#!/usr/bin/env python3
"""#48 regression guard: the WS-init failure `break` must not hit an unbound local.

The closed duplicate PR bound its dead-recorder flag INSIDE the cycle loop and
read it after the loop.  The WS-session-init failure path breaks out of the
loop before any per-cycle state exists, so that read raised UnboundLocalError
inside the STT worker thread -- the session never reported anything and never
went idle.  This pins the flag's binding site.

Expected: exactly one "STT session init failed" Error, no microphone Error,
idle, and NO exception escaping the worker thread.
exit 0 = clean, exit 1 = bug present.
"""
import sys
import threading
import time

import fakes
import harness

WINDOW = 6.0


def main():
    mod, _events, inj = harness.load()
    print(f"service under test: {harness.SVC_PATH}")

    errors = []
    thread_excs = []

    def hook(args):
        thread_excs.append(f"{args.exc_type.__name__}: {args.exc_value}")
    threading.excepthook = hook

    mod.HAS_WS = True
    mod.HAS_VAD = False
    mod.CONFIG["continuous_dictation"] = False
    mod._take_prewarmed_rec = lambda *a, **k: fakes.LiveProc()
    mod._get_stt_ws = lambda *a, **k: (
        fakes.ScriptedWS(mod.websocket.WebSocketTimeoutException), True)
    mod._make_ws_audio_msg = lambda *a, **k: b""
    mod._rest_stt_fallback = lambda *a, **k: ""
    mod._invalidate_stt_ws = lambda *a, **k: None
    mod._parse_ws_msg = lambda *a, **k: None

    def boom(*a, **k):
        raise RuntimeError("session init refused")
    mod._init_stt_ws_session = boom

    mod.GLib.idle_add = lambda fn, *a, **k: fn(*a, **k)

    svc = harness.make_service(mod)
    svc._stt_mode = "streaming"
    svc._reload_config_flags = lambda: None
    svc._emit_error = lambda msg: errors.append(msg) or False
    svc._try_cast = lambda text: False

    assert svc.start_listening() == "ok", "setup: start_listening refused"
    reached_idle = harness.wait_for(lambda: svc.current_state == "idle", WINDOW)
    time.sleep(0.3)
    svc.stop()
    time.sleep(0.3)

    failures = 0
    print(f"  Error signals: {errors}")
    print(f"  exceptions escaping the worker thread: {thread_excs}")
    print(f"  state: {svc.current_state!r}")
    if thread_excs:
        failures += 1
        print("    FAIL: the WS-init break path raised out of the worker")
    if len([e for e in errors if "session init failed" in e]) != 1:
        failures += 1
        print("    FAIL: expected exactly one session-init Error")
    if any("icrophone" in e for e in errors):
        failures += 1
        print("    FAIL: a healthy recorder was reported as a lost microphone")
    if not reached_idle:
        failures += 1
        print("    FAIL: service did not return to idle")
    leaked = svc._cancels.live()
    if leaked:
        failures += 1
        print(f"    FAIL: leaked cancel token(s) {leaked}")
    print()
    if failures:
        print(f"FAIL: {failures} -- WS-init break path is broken")
        return 1
    print("PASS: WS-init failure reports once, no unbound locals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
