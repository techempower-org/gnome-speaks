#!/usr/bin/env python3
"""#48: a prewarmed recorder that already exited must be discarded, not used.

_prewarm_recorder() starts pw-record and hands it over on the next hotkey
without ever polling it.  With an on-demand USB mic absent, pw-record exits
immediately, so the session opens on a corpse and diagnoses the failure
against a recorder that died minutes ago, in a different device state.

Expected: the stale process is discarded and a fresh recorder is started.
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

    errors = []
    spawned = []

    stale = fakes.DeadProc(exited=True)      # poll() is non-None from the start
    mod.HAS_WS = True
    mod.HAS_VAD = False
    mod.CONFIG["continuous_dictation"] = False
    mod._take_prewarmed_rec = lambda *a, **k: stale
    mod._get_stt_ws = lambda *a, **k: (
        fakes.ScriptedWS(mod.websocket.WebSocketTimeoutException), True)
    mod._make_ws_audio_msg = lambda *a, **k: b""
    mod._rest_stt_fallback = lambda *a, **k: ""
    mod._invalidate_stt_ws = lambda *a, **k: None
    mod._parse_ws_msg = lambda *a, **k: None
    mod._init_stt_ws_session = lambda *a, **k: None
    mod._build_rec_cmd = lambda *a, **k: ["/bin/true"]

    def fake_popen(cmd, **kw):
        spawned.append(cmd)
        return fakes.DeadProc()              # a fresh one, alive until read
    mod.subprocess.Popen = fake_popen

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
    print(f"  fresh recorders spawned: {len(spawned)}")
    print(f"  Error signals: {errors}")
    print(f"  state: {svc.current_state!r}")
    if len(spawned) != 1:
        failures += 1
        print("    FAIL: a stale prewarmed recorder was used instead of a fresh one")
    if not reached_idle:
        failures += 1
        print("    FAIL: service did not return to idle")
    leaked = svc._cancels.live()
    if leaked:
        failures += 1
        print(f"    FAIL: leaked cancel token(s) {leaked}")
    print()
    if failures:
        print(f"FAIL: {failures} -- stale prewarm handed to the session")
        return 1
    print("PASS: stale prewarmed recorder discarded, fresh one started")
    return 0


if __name__ == "__main__":
    sys.exit(main())
