#!/usr/bin/env python3
"""Call mute (JP, 2026-09-18): "nothing should ever speak when I'm on a video
call". Another process holding the microphone (a PipeWire capture stream) or
the camera (/dev/video*) is a call; while it lasts nothing plays -- agent
items are HELD in the queue (new POSTs get 503), the user's own speak(),
talk() and spell replies are refused/held, the wake watcher parks. Hotkey
dictation still types. Preference `mute_on_call`, default ON.

  C1  parser: GNOME Settings' "Peak detect" stream alone is not a call; a
      brave capture stream is; a corked brave stream still is (fails toward
      quiet); the service's own pw-record is ignored
  C2  probe: microphone before camera; camera holders detected; a holder in
      the ignore list is skipped
  C3  gates while on a call (mute_on_call on): POST /speak -> 503 naming the
      app; an item enqueued BEFORE the call is held (not played) and plays
      once the call ends; speak() False; talk() refused; a spell reply is held
  C4  mute_on_call off: on a call, POST /speak -> 200 and speak() plays
  C5  GET /status carries `call` {active, mute_on_call, by, what, since}
  C6  wiring: mute_on_call in _SYNC_FLAGS; prefs.js has the switch; the
      call-watcher thread exists

A tree without _call_muted() is reported as FAILING. exit 0/1.
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

PACTL_SETTINGS = '''Source Output #7621
\tDriver: PipeWire
\tCorked: no
\tProperties:
\t\tapplication.name = "GNOME Settings"
\t\tapplication.process.id = "1647258"
\t\tapplication.process.binary = "gnome-control-center"
\t\tmedia.name = "Peak detect"
'''
PACTL_BRAVE = '''Source Output #7801
\tCorked: no
\tProperties:
\t\tapplication.name = "Brave"
\t\tapplication.process.id = "424242"
\t\tapplication.process.binary = "brave"
\t\tmedia.name = "Chromium input"
'''
PACTL_BRAVE_CORKED = PACTL_BRAVE.replace("Corked: no", "Corked: yes")
PACTL_OWN = '''Source Output #7900
\tCorked: no
\tProperties:
\t\tapplication.name = "pw-record"
\t\tapplication.process.id = "%d"
\t\tapplication.process.binary = "pw-record"
''' % os.getpid()


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
    mod.SpeechHTTPHandler.service = svc
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), mod.SpeechHTTPHandler)
    port = srv.server_address[1]
    assert port != 7710
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def req(method, path, payload=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        body = json.dumps(payload).encode() if payload is not None else None
        c.request(method, path, body=body, headers={"Content-Type": "application/json"})
        r = c.getresponse(); out = json.loads(r.read()); c.close()
        return r.status, out
    return srv, req


def recent(svc, item_id):
    for e in list(svc._queue_recent):
        if e.get("id") == item_id:
            return e.get("outcome")
    return None


def main():
    print(f"service under test: {harness.SVC_PATH}")
    mod, events, _inj = harness.load(fake_tts_seconds=0.2)
    if not hasattr(mod.GnomeSpeaksService, "_call_muted"):
        print("FAIL: this tree has no call mute (GnomeSpeaksService._call_muted) -- "
              "speech plays into video calls")
        return 1
    P = mod._parse_source_outputs
    svc = harness.make_service(mod)
    srv, req = serve(mod, svc)
    C = mod.CONFIG

    # Pin the probe: the real watcher thread polls the desktop; here the inputs
    # are ours. The thread keeps running but only ever sees what we feed it.
    streams, cams = [], []
    svc._list_capture_streams = lambda: list(streams)
    svc._list_camera_holders = lambda: list(cams)
    # Safety net: talk() past its gate reaches speech_tts.talk_fullduplex --
    # the REAL Azure full-duplex path and the microphone. A gate miss must
    # fail fast here, never hang a repro on the network (it did, 2026-09-18).
    def _no_talk(*a, **k):
        raise AssertionError("talk() reached talk_fullduplex -- the call gate did not hold")
    mod.speech_tts.talk_fullduplex = _no_talk

    def on_call(hit):
        """Plant a call THROUGH the detector and wait for the watcher's write.
        Writing svc._on_call directly races the watcher, which polls every
        CALL_POLL_SECONDS and overwrites it with what the detector says."""
        streams[:] = P(PACTL_BRAVE) if hit else []
        cams[:] = []
        ok = wait_for(lambda: (svc._on_call is not None) == bool(hit),
                      mod.CALL_POLL_SECONDS * 2 + 1)
        assert ok, f"watcher did not settle to on_call={bool(hit)}"

    # ---- C1 parser ------------------------------------------------------------
    ign = lambda text: [not mod._call_ignored(s["binary"], s["name"], s["pid"]) for s in P(text)]
    check("C1", ign(PACTL_SETTINGS) == [False], "GNOME Settings peak detect is ignored")
    check("C1", ign(PACTL_BRAVE) == [True], "a brave capture stream counts")
    check("C1", ign(PACTL_BRAVE_CORKED) == [True] and P(PACTL_BRAVE_CORKED)[0]["corked"] is True,
          "a corked brave stream still counts (fails toward quiet)")
    check("C1", ign(PACTL_OWN) == [False], "the service's own pw-record is ignored")
    both = P(PACTL_SETTINGS + PACTL_BRAVE)
    check("C1", len(both) == 2 and both[1]["binary"] == "brave" and both[1]["pid"] == 424242,
          f"two blocks parse independently ({[(b['binary'], b['pid']) for b in both]})")

    # ---- C2 probe -------------------------------------------------------------
    streams[:] = P(PACTL_SETTINGS); cams[:] = []
    check("C2", svc._probe_call() is None, "settings alone -> no call")
    streams[:] = P(PACTL_SETTINGS + PACTL_BRAVE); cams[:] = [("cheese", 5)]
    check("C2", svc._probe_call() == ("brave", "microphone"), "microphone holder reported first")
    streams[:] = []; cams[:] = [("pipewire", 3), ("brave", 424242)]
    check("C2", svc._probe_call() == ("brave", "camera"), "camera holder detected, pipewire ignored")
    streams[:] = []; cams[:] = []
    check("C2", svc._probe_call() is None, "nothing held -> no call")

    # ---- C3 gates -------------------------------------------------------------
    C["mute_on_call"] = True
    on_call(True)
    # "Before the call" for the dispatcher: the hold is judged at dequeue, so an
    # item that arrives while muted (via enqueue_speech, the spell path -- not
    # POST /speak, which refuses) is exactly the pre-call case.
    item_id, _p, _d = svc.enqueue_speech("Queued before the call", source="repro")
    time.sleep(0.6)
    check("C3", recent(svc, item_id) is None and events == [] and svc._tts_queue.qsize() == 1,
          f"pre-call item HELD, not played (queue={svc._tts_queue.qsize()}, events={events})")
    status, out = req("POST", "/speak", {"text": "agent line during a call", "source": "repro"})
    check("C3", status == 503 and "brave" in json.dumps(out) and "call" in json.dumps(out).lower(),
          f"POST /speak -> {status} {out}")
    ok = svc.speak("User speak during a call")
    check("C3", ok is False and events == [], f"speak() refused (ok={ok})")
    r = svc.talk("Anyone there?")
    check("C3", isinstance(r, str) and r.startswith("error:") and "call" in r.lower(),
          f"talk() refused: {r!r}")
    svc._spell_speak("Spell reply during a call")
    time.sleep(0.5)
    check("C3", events == [] and svc._tts_queue.qsize() == 2, "spell reply held with the queue")
    on_call(False)                                         # the call ends
    done = wait_for(lambda: recent(svc, item_id) == "done", 6.0)
    check("C3", done and any(e[0] == "start" and e[1] == "Queued" for e in events),
          f"held item plays once the call ends (outcome={recent(svc, item_id)!r})")
    wait_for(lambda: svc._tts_queue.qsize() == 0 and svc.current_state == "idle", 5.0)

    # ---- C4 preference off -------------------------------------------------------
    del events[:]
    on_call(True)              # detected while the pref is on...
    C["mute_on_call"] = False  # ...then the pref is turned off mid-call
    status, out = req("POST", "/speak", {"text": "allowed with the pref off", "source": "repro"})
    check("C4", status == 200, f"mute_on_call=false: POST /speak -> {status}")
    wait_for(lambda: any(e[0] == "start" and e[1] == "allowed" for e in events), 4.0)
    wait_for(lambda: svc.current_state == "idle", 4.0)
    ok = svc.speak("Hello with the pref off")
    played = wait_for(lambda: any(e[0] == "start" and e[1] == "Hello" for e in events), 3.0)
    check("C4", ok is True and played, f"speak() plays with the pref off (ok={ok})")
    wait_for(lambda: svc.current_state == "idle", 3.0)
    cleared = wait_for(lambda: svc._on_call is None, mod.CALL_POLL_SECONDS * 2 + 1)
    check("C4", cleared, "pref off: the watcher stops probing and clears the call state")

    # ---- C5 status -----------------------------------------------------------------
    C["mute_on_call"] = True
    on_call(True)          # the watcher resumes probing with the pref back on
    status, out = req("GET", "/status")
    ci = out.get("call") or {}
    check("C5", status == 200 and ci.get("active") is True and ci.get("by") == "brave"
          and ci.get("what") == "microphone" and ci.get("mute_on_call") is True and "since" in ci,
          f"/status call={ci}")
    on_call(False)

    # ---- C6 wiring -----------------------------------------------------------------
    prefs = open(os.path.join(os.path.dirname(harness.SVC_PATH), "prefs.js")).read()
    thread = any(t.name == "call-watcher" for t in threading.enumerate())
    check("C6", "mute_on_call" in mod.GnomeSpeaksService._SYNC_FLAGS
          and "'mute_on_call'" in prefs and thread,
          f"_SYNC_FLAGS ok, prefs row ok, call-watcher thread alive={thread}")

    srv.shutdown()
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s): {sorted(set(FAILS))}")
        return 1
    print("PASS: a foreign mic/camera holder is a call; while it lasts nothing plays and agent "
          "items wait; the preference turns it off; status names the app")
    return 0


if __name__ == "__main__":
    sys.exit(main())
