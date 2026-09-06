#!/usr/bin/env python3
"""Issue #57: a dead recorder is invisible -- no Error signal, loop mode spins.

The recorder (pw-record on a USB mic that is unplugged) returns EOF from
stdout immediately.  Expected: exactly one Error signal, the cycle loop exits
after one cycle, the service is idle.  Bug: no Error, and with
continuous_dictation=True the cycle loop treats the EOF as "no speech" and
re-enters listening -- forever, badge stuck on "listening".

exit 0 = clean, exit 1 = bug present.
"""
import sys
import threading
import time

import harness

WINDOW = 6.0   # a spin produces several cycles in this window (~2s each)


class DeadStdout:
    def __init__(self, proc):
        self._proc = proc

    def read(self, n):
        self._proc.exited = True     # pw-record exits right after its EOF
        return b""


class DeadProc:
    """pw-record whose device vanished mid-session: stdout EOF, then exit.

    poll() reports alive until the first read so the prewarm liveness check
    passes -- this is the mic yanked while the session is live, not a stale
    prewarm (that case is covered by the same check and must not open a
    real recorder from inside the repro).
    """

    def __init__(self):
        self.exited = False
        self.stdout = DeadStdout(self)

    def poll(self):
        return 1 if self.exited else None

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 1


class FakeWS:
    def __init__(self, timeout_exc):
        self._exc = timeout_exc

    def settimeout(self, t):
        pass

    def send(self, payload, opcode=None):
        pass

    def recv(self):
        time.sleep(0.05)
        raise self._exc()


def main():
    mod, _events, inj = harness.load()
    print(f"service under test: {harness.SVC_PATH}")

    cycles = [0]
    errors = []

    mod.HAS_WS = True
    mod.HAS_VAD = False
    mod.CONFIG["continuous_dictation"] = True
    mod._take_prewarmed_rec = lambda *a, **k: DeadProc()
    mod._get_stt_ws = lambda *a, **k: (FakeWS(mod.websocket.WebSocketTimeoutException), True)
    mod._make_ws_audio_msg = lambda *a, **k: b""
    mod._rest_stt_fallback = lambda *a, **k: ""
    mod._parse_ws_msg = lambda *a, **k: None
    # the real calibrate_noise is left in place: (500.0, []) on a dead pipe

    def count_cycle(*a, **k):
        cycles[0] += 1
    mod._init_stt_ws_session = count_cycle

    # No GLib main loop in the harness: run idle callbacks inline so the
    # Error signal (marshalled via idle_add) is observable.
    mod.GLib.idle_add = lambda fn, *a, **k: fn(*a, **k)

    svc = harness.make_service(mod)
    svc._stt_mode = "streaming"
    svc._reload_config_flags = lambda: None   # keep continuous_dictation=True
    svc._emit_error = lambda msg: errors.append(msg) or False

    assert svc.start_listening() == "ok", "setup: start_listening refused"
    idle_at = [None]
    t0 = time.monotonic()
    if harness.wait_for(lambda: svc.current_state == "idle", WINDOW):
        idle_at[0] = time.monotonic() - t0
    else:
        time.sleep(0.2)
    state_seen = svc.current_state
    svc.stop()
    time.sleep(0.3)

    failures = 0
    print(f"  cycles run in {WINDOW:.0f}s window: {cycles[0]}")
    print(f"  Error signals: {errors}")
    print(f"  state after window: {state_seen!r}"
          + (f" (idle after {idle_at[0]:.1f}s)" if idle_at[0] is not None else ""))
    if cycles[0] > 1:
        failures += 1
        print("    FAIL: loop mode spun on a dead recorder")
    if len(errors) != 1 or "icrophone" not in errors[0]:
        failures += 1
        print("    FAIL: expected exactly one microphone Error signal")
    if idle_at[0] is None or idle_at[0] > 4.0:
        failures += 1
        print("    FAIL: service did not return to idle promptly")
    if inj.all_text():
        failures += 1
        print(f"    FAIL: text reached the cursor: {inj.all_text()}")
    leaked = svc._cancels.live()
    if leaked:
        failures += 1
        print(f"    FAIL: leaked cancel token(s) {leaked}")
    print()
    if failures:
        print(f"FAIL: {failures} -- a dead recorder is invisible / spins")
        return 1
    print("PASS: dead recorder -> one Error, one cycle, idle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
