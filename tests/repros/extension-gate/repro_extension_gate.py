#!/usr/bin/env python3
"""The extension is the master switch (JP, 2026-09-10): while the GNOME Speaks
extension is disabled, the service must not open the mic, play any speech, or
type. Nothing.

Why a service-side gate at all: the service is a systemd user unit and every
actuation path lives in it -- the wake watcher, the speech queue, D-Bus Speak /
Talk / StartListening, loop restarts. On 2026-09-10 the extension was disabled
and the wake model (streaming the room off a webcam mic) false-fired twice in
13 minutes; the persisted loop flag turned each into an open mic that typed a
private phone call through ydotool for minutes at a time, with no badge, no
Loop pill and no tap-to-stop -- the extension being off had removed every
indicator and left every actuator running. Disabling the extension has to
mean OFF.

The instrument is the session-bus name `org.gnome.Speaks.Desktop`: the
extension owns it in enable() and releases it in disable() (and it vanishes
with a shell crash), so ownership IS "the extension is loaded". The service
watches it (`_extension_present`), and `_extension_gate()` is the ONE verdict
every seam reads. `REQUIRE_EXTENSION` (env `GS_REQUIRE_EXTENSION=0`) is the
seam for headless use and for these harnesses -- isolation.isolate_config()
declares the extension present so every other suite keeps measuring what it
always measured.

  X1  absent: start_listening() refused, names the extension, state idle,
      no STT thread -- the gate runs BEFORE anything can spawn a recorder
  X2  absent: speak() returns False, no TTS, hold depth 0
  X3  absent: a queued item is dropped with outcome "suppressed", never
      played (dropped, not HELD -- a burst of stale speech when the extension
      comes back is the coalescing rule's exact anti-goal)
  X4  absent: talk() refused, names the extension, no TTS
  X5  absent: POST /speak answers 503 and enqueues nothing (same rule as a
      service that cannot speak: never 200 and then silence)
  X6  GET /status carries `extension` (false absent, true present)
  X7  present: speak() and a queued item both play -- the positive control
  X8  REQUIRE_EXTENSION=False, absent: speak() plays -- the seam's control
  X9  _extension_gate() is None present, words absent

A tree without `_extension_gate` is reported as FAILING without calling
start_listening(): on such a tree X1 would open the REAL microphone.

exit 0 = all hold; 1 = at least one violated.
"""
import http.client
import http.server
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "cancel-tokens"))

import harness  # noqa: E402

FAILS = []


def check(label, ok, msg):
    print(f"    {'ok  ' if ok else 'FAIL'}: {label} {msg}")
    if not ok:
        FAILS.append(label)


def wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def serve(mod, svc):
    """A real SpeechHTTPHandler on an ephemeral port -- never 7710."""
    mod.SpeechHTTPHandler.service = svc
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), mod.SpeechHTTPHandler)
    port = srv.server_address[1]
    assert port != 7710
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def req(method, path, payload=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        body = json.dumps(payload).encode() if payload is not None else None
        c.request(method, path, body=body,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        out = json.loads(r.read())
        c.close()
        return r.status, out

    return srv, req


def recent(svc, item_id):
    for entry in list(svc._queue_recent):
        if entry.get("id") == item_id:
            return entry.get("outcome")
    return None


def stt_alive(svc):
    with svc._stt_lock:
        t = svc._stt_thread
    return t is not None and t.is_alive()


def main():
    print(f"service under test: {harness.SVC_PATH}")
    mod, events, _inj = harness.load(fake_tts_seconds=0.3)
    if not hasattr(mod, "_extension_gate") or not hasattr(mod, "REQUIRE_EXTENSION"):
        print("FAIL: this tree has no extension gate (_extension_gate / "
              "REQUIRE_EXTENSION) -- every actuator runs with the extension "
              "disabled. Refusing to call start_listening() here: it would "
              "open the real microphone.")
        return 1
    svc = harness.make_service(mod)
    srv, req = serve(mod, svc)

    # ---- absent -----------------------------------------------------------
    mod.REQUIRE_EXTENSION = True
    mod._extension_present = False

    r = svc.start_listening()
    check("X1", isinstance(r, str) and r.startswith("error:") and "extension" in r.lower(),
          f"start_listening refused and names the extension: {r!r}")
    check("X1", svc.current_state == "idle", f"state stays idle (got {svc.current_state})")
    check("X1", not stt_alive(svc), "no STT thread was started")

    ok = svc.speak("Hello from the gate")
    time.sleep(0.2)
    check("X2", ok is False, f"speak() returns False (got {ok!r})")
    check("X2", events == [], f"no TTS ran (events {events})")
    check("X2", svc._user_speech_depth == 0,
          f"user-speech hold released (depth {svc._user_speech_depth})")
    check("X2", svc.current_state == "idle", f"state idle (got {svc.current_state})")

    item_id, _pos, _dropped = svc.enqueue_speech("Queued while off", source="repro")
    settled = wait_for(lambda: recent(svc, item_id) is not None, timeout=3.0)
    check("X3", settled and recent(svc, item_id) == "suppressed",
          f"queued item outcome is 'suppressed' (got {recent(svc, item_id)!r})")
    check("X3", events == [], f"queued item never played (events {events})")
    check("X3", svc._tts_queue.qsize() == 0,
          f"queue is empty -- dropped, not held (depth {svc._tts_queue.qsize()})")
    check("X3", svc.current_state == "idle", f"state idle (got {svc.current_state})")

    r = svc.talk("Anyone there?")
    check("X4", isinstance(r, str) and r.startswith("error:") and "extension" in r.lower(),
          f"talk() refused and names the extension: {r!r}")
    check("X4", events == [], f"no TTS ran (events {events})")
    check("X4", svc._user_speech_depth == 0,
          f"user-speech hold released (depth {svc._user_speech_depth})")

    status, out = req("POST", "/speak", {"text": "HTTP while off", "source": "repro"})
    check("X5", status == 503, f"POST /speak -> 503 (got {status} {out})")
    check("X5", "extension" in json.dumps(out).lower(), f"body names the extension: {out}")
    check("X5", svc._tts_queue.qsize() == 0, "nothing enqueued")

    status, out = req("GET", "/status")
    check("X6", status == 200 and out.get("extension") is False,
          f"GET /status extension=false while absent (got {out.get('extension')!r})")

    gate = mod._extension_gate()
    check("X9", isinstance(gate, str) and gate, f"gate words while absent: {gate!r}")

    # ---- present: the positive control ------------------------------------
    mod._extension_present = True
    check("X9", mod._extension_gate() is None, "gate is None while present")

    status, out = req("GET", "/status")
    check("X6", status == 200 and out.get("extension") is True,
          f"GET /status extension=true while present (got {out.get('extension')!r})")

    ok = svc.speak("Hello for real")
    started = wait_for(lambda: any(e[0] == "start" and e[1] == "Hello" for e in events))
    check("X7", ok is True and started, f"speak() plays while present (ok={ok}, events {events})")
    wait_for(lambda: any(e[0] == "end" for e in events), timeout=3.0)
    wait_for(lambda: svc.current_state == "idle", timeout=3.0)

    del events[:]
    item_id, _pos, _dropped = svc.enqueue_speech("Queued for real", source="repro")
    done = wait_for(lambda: recent(svc, item_id) == "done", timeout=4.0)
    check("X7", done, f"queued item plays while present (outcome {recent(svc, item_id)!r})")
    check("X7", any(e[0] == "start" and e[1] == "Queued" for e in events),
          f"the fake TTS saw it (events {events})")
    wait_for(lambda: svc.current_state == "idle", timeout=3.0)

    # ---- the headless seam ---------------------------------------------------
    del events[:]
    mod._extension_present = False
    mod.REQUIRE_EXTENSION = False
    check("X8", mod._extension_gate() is None, "REQUIRE_EXTENSION=False: gate is None while absent")
    ok = svc.speak("Headless hello")
    started = wait_for(lambda: any(e[0] == "start" and e[1] == "Headless" for e in events))
    check("X8", ok is True and started, f"speak() plays headless (ok={ok}, events {events})")
    wait_for(lambda: svc.current_state == "idle", timeout=3.0)

    srv.shutdown()
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s): {sorted(set(FAILS))}")
        return 1
    print("PASS: with the extension absent nothing listens, speaks or types; "
          "present, everything does; the headless seam bypasses the gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
