#!/usr/bin/env python3
"""Quiet hours (JP, 2026-09-12): a scheduled window in which AGENT speech --
POST /speak -- is refused with 503, while everything the user does themself
(dictation, D-Bus Speak, spell replies, /cast, /respeak) is untouched.

  Q1  window math: overnight 22:00-08:00 (23:00 and 03:00 in, 12:00 out,
      22:00 in, 08:00 out), same-day 13:00-15:00, start==end never, schedule
      off never, garbage HH:MM falls back to the default window
  Q2  HTTP: inside the window POST /speak -> 503 naming quiet hours and
      nothing is enqueued; the user's own speak() still plays; a spell reply
      still rides the queue; schedule off -> /speak 200
  Q3  override: toggle inside the window -> inactive until the scheduled
      end, expires there; toggle outside the window -> active until the
      scheduled start
  Q4  override with the schedule off lasts 24 h
  Q5  GET /status carries `quiet` {active, enabled, scheduled, window, override}
  Q6  spellbook: "cast quiet hours" hits the quiet-hours spell (longest
      pattern wins over the silence spell's bare "quiet"); "cast quiet" is
      still silence; the dbus_self op answers in words and flips the verdict
  Q7  wiring: the three keys are in _SYNC_FLAGS (prefs edits land live) and
      both D-Bus XMLs (service + extension.js) declare Toggle/GetQuietHours

A tree without quiet_hours_active() is reported as FAILING. exit 0/1.
"""
import datetime as dt
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


def T(h, m=0, day=1):
    return dt.datetime(2026, 9, day, h, m)


def main():
    print(f"service under test: {harness.SVC_PATH}")
    mod, events, _inj = harness.load(fake_tts_seconds=0.2)
    if not hasattr(mod.GnomeSpeaksService, "quiet_hours_active"):
        print("FAIL: this tree has no quiet hours (GnomeSpeaksService.quiet_hours_active)")
        return 1
    svc = harness.make_service(mod)
    srv, req = serve(mod, svc)
    C = mod.CONFIG

    def window(enabled, start, end):
        C.update({"quiet_hours": enabled, "quiet_hours_start": start, "quiet_hours_end": end})
        svc._quiet_override = None

    # ---- Q1 -----------------------------------------------------------------
    window(True, "22:00", "08:00")
    q = svc.quiet_hours_active
    check("Q1", q(T(23)) and q(T(3)) and not q(T(12)) and q(T(22, 0)) and not q(T(8, 0)),
          f"overnight: 23h={q(T(23))} 03h={q(T(3))} 12h={q(T(12))} 22:00={q(T(22))} 08:00={q(T(8))}")
    window(True, "13:00", "15:00")
    check("Q1", q(T(14)) and not q(T(16)) and not q(T(12, 59)) and q(T(13)),
          f"same-day 13-15: 14h={q(T(14))} 16h={q(T(16))} 12:59={q(T(12,59))}")
    window(True, "09:00", "09:00")
    check("Q1", not q(T(9)) and not q(T(10)), "start==end is an empty window")
    window(False, "00:00", "23:59")
    check("Q1", not q(T(12)), "schedule off -> never active")
    window(True, "garbage", None)
    check("Q1", q(T(23)) and not q(T(12)) and svc.quiet_hours_info(T(23))["window"] == "22:00-08:00",
          f"bad HH:MM falls back to 22:00-08:00 (window={svc.quiet_hours_info(T(23))['window']})")

    # ---- Q2: HTTP uses the real clock -> build a window around now ----------
    now = dt.datetime.now()
    start = (now - dt.timedelta(hours=1)).strftime("%H:%M")
    end = (now + dt.timedelta(hours=1)).strftime("%H:%M")
    window(True, start, end)
    check("Q2", svc.quiet_hours_active(), f"window {start}-{end} covers now")
    status, out = req("POST", "/speak", {"text": "agent status while quiet", "source": "repro"})
    check("Q2", status == 503 and "quiet hours" in json.dumps(out).lower(),
          f"POST /speak -> {status} {out}")
    check("Q2", svc._tts_queue.qsize() == 0, "nothing enqueued")
    ok = svc.speak("Hello from the user")
    started = wait_for(lambda: any(e[0] == "start" and e[1] == "Hello" for e in events))
    check("Q2", ok is True and started, f"user's own speak() plays during quiet hours (ok={ok})")
    wait_for(lambda: svc.current_state == "idle", 3.0)
    del events[:]
    svc._spell_speak("Spell reply while quiet")
    spoke = wait_for(lambda: any(e[0] == "start" and e[1] == "Spell" for e in events), 4.0)
    check("Q2", spoke, f"a spell reply still rides the queue (events {events})")
    wait_for(lambda: svc.current_state == "idle", 3.0)
    window(False, start, end)
    status, out = req("POST", "/speak", {"text": "agent status now allowed", "source": "repro"})
    check("Q2", status == 200 and out.get("ok") is True, f"schedule off -> POST /speak {status}")
    wait_for(lambda: svc._tts_queue.qsize() == 0 and svc.current_state == "idle", 4.0)

    # ---- Q3: override against a fixed clock ----------------------------------
    window(True, "22:00", "08:00")
    t = T(23, 30)
    new = svc.toggle_quiet_hours(now=t)
    info = svc.quiet_hours_info(t)
    check("Q3", new is False and not svc.quiet_hours_active(t) and info["override"]
          and info["override_until"] == "08:00",
          f"toggle inside the window -> off until the scheduled end (info={info})")
    check("Q3", not svc.quiet_hours_active(T(7, 59, day=2)) and not svc.quiet_hours_active(T(8, 1, day=2))
          and svc._quiet_override is None,
          "override holds to 07:59, expires at 08:00, then the schedule (now outside) applies")
    svc._quiet_override = None
    t = T(12)
    new = svc.toggle_quiet_hours(now=t)
    info = svc.quiet_hours_info(t)
    refusal = svc._quiet_hours_refusal(t)
    check("Q3", "until 22:00" in (refusal or ""), f"refusal names the end: {refusal!r}")
    # Probing a clock PAST the override's expiry clears the override (that is
    # the expiry) -- so it comes last.
    check("Q3", new is True and svc.quiet_hours_active(t) and info["override_until"] == "22:00"
          and svc.quiet_hours_active(T(21, 59)) and svc.quiet_hours_active(T(22, 30))
          and svc._quiet_override is None,
          f"toggle outside -> quiet until 22:00, then the schedule keeps it quiet (info={info})")

    # ---- Q4 -----------------------------------------------------------------
    window(False, "22:00", "08:00")
    t = T(12)
    new = svc.toggle_quiet_hours(now=t)
    check("Q4", new is True and svc.quiet_hours_active(t + dt.timedelta(hours=23))
          and not svc.quiet_hours_active(t + dt.timedelta(hours=25)),
          "schedule off: override lasts 24 h")
    svc._quiet_override = None

    # ---- Q5 -----------------------------------------------------------------
    window(True, "22:00", "08:00")
    status, out = req("GET", "/status")
    qi = out.get("quiet") or {}
    check("Q5", status == 200 and {"active", "enabled", "scheduled", "window", "override"} <= set(qi)
          and qi["window"] == "22:00-08:00", f"/status quiet={qi}")

    # ---- Q6 -----------------------------------------------------------------
    sb = mod.spellbook
    kind, spell, _ = sb.match("cast quiet hours", svc._spellbook)
    kind2, spell2, _ = sb.match("cast quiet", svc._spellbook)
    check("Q6", kind == "cast" and spell["name"] == "quiet-hours" and kind2 == "cast"
          and spell2["name"] == "silence",
          f"'cast quiet hours' -> {spell and spell['name']}, 'cast quiet' -> {spell2 and spell2['name']}")
    svc._quiet_override = None
    before = svc.quiet_hours_active()
    words = svc._spell_ctx_dbus("quiet_hours_toggle")
    after = svc.quiet_hours_active()
    check("Q6", isinstance(words, str) and "quiet hours" in words.lower() and after != before,
          f"op flips {before}->{after}, says {words!r}")
    svc._quiet_override = None

    # ---- Q7 -----------------------------------------------------------------
    flags = set(mod.GnomeSpeaksService._SYNC_FLAGS)
    ext = open(os.path.join(os.path.dirname(harness.SVC_PATH), "extension.js")).read()
    check("Q7", {"quiet_hours", "quiet_hours_start", "quiet_hours_end"} <= flags
          and "ToggleQuietHours" in mod.INTROSPECTION_XML and "GetQuietHours" in mod.INTROSPECTION_XML
          and "ToggleQuietHours" in ext and "GetQuietHours" in ext,
          "keys in _SYNC_FLAGS; Toggle/GetQuietHours in both D-Bus XMLs")

    srv.shutdown()
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s): {sorted(set(FAILS))}")
        return 1
    print("PASS: quiet hours refuse agent speech at the door, leave the user's voice alone, "
          "override until the next boundary, and are wired through prefs, D-Bus and the spellbook")
    return 0


if __name__ == "__main__":
    sys.exit(main())
