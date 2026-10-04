#!/usr/bin/env python3
"""HTTP API admission (#186, phase 1): the loopback API answers agents and
curl exactly as before, and refuses what a web page in a local browser can
send.

  A1  Origin: a foreign Origin -> 403 (envelope) on POST /speak with NOTHING
      enqueued, on GET /chronicle, and on OPTIONS; the same POST with no
      Origin -> 200 (the agent path); an allowlisted Origin -> 200 with its
      own Access-Control-Allow-Origin echoed (never "*"); no Origin -> no
      CORS headers at all
  A2  Host: a forged Host name, or a loopback name on the wrong port -> 403;
      127.0.0.1:<port>, localhost:<port>, [::1]:<port> and bare 127.0.0.1
      are admitted
  A3  Content-Type is a BROWSER rule: with no Origin (agents, `curl -d`,
      speak.sh) a JSON body sent as text/plain, form-urlencoded or with no
      Content-Type -> 200; with an allowlisted Origin the same requests ->
      415 with nothing enqueued, application/json with a charset -> 200, a
      bodyless POST -> 200; a foreign Origin with text/plain -> 403 (the
      Origin check answers first)
  A4  output_file: '../', an absolute path outside, and a symlink inside the
      directory pointing outside -> 400 and nothing written anywhere; a plain
      name -> 200 and the file is written inside the confined directory
  A5  POST /cast honours the master switch like a spoken cast: off -> 503
      and the spellbook is never consulted; quiet hours and call mute stay
      exempt (2026-09-12 status quo) -> 200, it casts
  A6  GET /api/version carries no hostname

Hermetic: an ephemeral port (asserted != 7710), no live service, scratch in
the repo's gitignored tmp/ keyed by PID. exit 0 = all hold; 1 = a violation.
"""
import datetime as dt
import http.client
import http.server
import json
import os
import shutil
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "cancel-tokens"))
import harness  # noqa: E402

FAILS = []
FOREIGN = "http://evil.example"
ALLOWED = "http://allowed.example:8080"


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


def main():
    print(f"service under test: {harness.SVC_PATH}")
    mod, events, _inj = harness.load(fake_tts_seconds=0.05)
    if not hasattr(mod, "_confine_output_file") or not hasattr(mod, "HTTP_ALLOWED_ORIGINS"):
        print("FAIL: this tree has no HTTP admission (HTTP_ALLOWED_ORIGINS / "
              "_confine_output_file)")
        return 1

    # Confined output directory lives in scratch, never ~/.cache.
    cache = os.path.join(harness.SCRATCH, "cache")
    outside = os.path.join(harness.SCRATCH, "outside")
    os.makedirs(outside, exist_ok=True)
    os.environ["XDG_CACHE_HOME"] = cache
    out_dir = mod._output_dir()
    assert out_dir.startswith(harness.SCRATCH), out_dir

    # A fake TTS that WRITES output_file the way speech_tts does, so "nothing
    # written" and "written" are facts on disk, not on a mock.
    writes = []

    def fake_tts(text, **kw):
        path = kw.get("output_file")
        if path:
            path = os.path.expanduser(path)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"RIFF-fake")
            writes.append(path)
        events.append(("start", text.split()[0], time.monotonic()))
        return {"spoken": True}
    harness.isolation.install_fake_tts(mod, fake_tts)

    mod.HTTP_ALLOWED_ORIGINS = mod._parse_origin_allowlist(ALLOWED + "/, http://other.example")
    svc = harness.make_service(mod)
    mod.SpeechHTTPHandler.service = svc
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), mod.SpeechHTTPHandler)
    port = srv.server_address[1]
    assert port != 7710
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    enq = []
    real_enqueue = svc.enqueue_speech

    def spy_enqueue(*a, **k):
        enq.append((a, k))
        return real_enqueue(*a, **k)
    svc.enqueue_speech = spy_enqueue

    casts = []
    real_try_cast = svc._try_cast

    def spy_cast(text):
        casts.append(text)
        return real_try_cast(text)
    svc._try_cast = spy_cast

    def raw(method, path, body=None, headers=None, host=None):
        """One request; `host` overrides the Host header verbatim."""
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.putrequest(method, path, skip_host=host is not None,
                     skip_accept_encoding=True)
        if host is not None:
            c.putheader("Host", host)
        hdrs = dict(headers or {})
        if body is not None:
            hdrs.setdefault("Content-Length", str(len(body)))
        for k, v in hdrs.items():
            c.putheader(k, v)
        c.endheaders(body)
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            out = json.loads(data) if data else None
        except ValueError:
            out = data
        return r.status, out, {k.lower(): v for k, v in r.getheaders()}

    def post(path, payload, headers=None, **kw):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        return raw("POST", path, json.dumps(payload).encode(), h, **kw)

    def envelope(out):
        return isinstance(out, dict) and out.get("ok") is False and isinstance(out.get("error"), str)

    def settle():
        wait_for(lambda: svc._tts_queue.qsize() == 0 and svc.current_state == "idle", 4.0)

    # ---- A1 Origin ----------------------------------------------------------
    st, out, h = post("/speak", {"text": "Foreign hello", "source": "repro"},
                      {"Origin": FOREIGN})
    check("A1", st == 403 and envelope(out) and not enq and svc._tts_queue.qsize() == 0,
          f"foreign Origin POST /speak -> {st} {out}, enqueued={len(enq)}")
    check("A1", "access-control-allow-origin" not in h, f"no CORS on refusal: {h}")
    st, out, h = raw("GET", "/chronicle", headers={"Origin": FOREIGN})
    check("A1", st == 403 and envelope(out), f"foreign Origin GET /chronicle -> {st}")
    st, out, h = raw("OPTIONS", "/speak", headers={
        "Origin": FOREIGN, "Access-Control-Request-Method": "POST"})
    check("A1", st == 403 and "access-control-allow-origin" not in h,
          f"foreign preflight -> {st} {h.get('access-control-allow-origin')!r}")
    st, out, h = post("/speak", {"text": "Agent hello", "source": "repro"})
    check("A1", st == 200 and out.get("ok") is True and len(enq) == 1,
          f"no Origin (agent path) -> {st} {out}")
    check("A1", "access-control-allow-origin" not in h, "no Origin -> no CORS headers")
    settle()
    st, out, h = post("/speak", {"text": "Allowed hello"}, {"Origin": ALLOWED})
    check("A1", st == 200 and h.get("access-control-allow-origin") == ALLOWED,
          f"allowlisted Origin -> {st}, ACAO={h.get('access-control-allow-origin')!r}")
    settle()
    st, out, h = raw("OPTIONS", "/speak", headers={
        "Origin": ALLOWED, "Access-Control-Request-Method": "POST"})
    check("A1", st == 204 and h.get("access-control-allow-origin") == ALLOWED,
          f"allowlisted preflight -> {st} ACAO={h.get('access-control-allow-origin')!r}")
    st, out, h = raw("GET", "/status", headers={"Origin": "null"})
    check("A1", st == 403, f"Origin: null -> {st}")

    # ---- A2 Host ------------------------------------------------------------
    n = len(enq)
    for bad in (f"evil.example:{port}", "evil.example", f"127.0.0.1:{port + 1}",
                f"localhost.evil.example:{port}"):
        st, out, _ = post("/speak", {"text": "Rebound hello"}, host=bad)
        check("A2", st == 403 and envelope(out) and len(enq) == n,
              f"Host {bad!r} -> {st}")
    for good in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}",
                 "127.0.0.1", f"LOCALHOST:{port}"):
        st, out, _ = raw("GET", "/status", host=good)
        check("A2", st == 200, f"Host {good!r} -> {st}")

    # ---- A3 Content-Type ----------------------------------------------------
    body = json.dumps({"text": "Plain hello"}).encode()
    # No Origin: never Content-Type-checked (`curl -d` is form-urlencoded).
    for ctype in ("text/plain", "application/x-www-form-urlencoded", None):
        n = len(enq)
        st, out, _ = raw("POST", "/speak", body, {"Content-Type": ctype} if ctype else {})
        check("A3", st == 200 and out.get("ok") is True and len(enq) == n + 1,
              f"no Origin, {ctype or 'no Content-Type'} JSON body -> {st}")
        settle()
    st, out, _ = raw("POST", "/stop")
    check("A3", st == 200 and out.get("ok") is True,
          f"no Origin, bodyless POST /stop -> {st} {out}")
    # Allowlisted Origin: a body must be application/json.
    n = len(enq)
    for ctype in ("text/plain", "application/x-www-form-urlencoded",
                  "multipart/form-data; boundary=x", None):
        hdrs = {"Origin": ALLOWED}
        if ctype:
            hdrs["Content-Type"] = ctype
        st, out, _ = raw("POST", "/speak", body, hdrs)
        check("A3", st == 415 and envelope(out) and len(enq) == n,
              f"allowlisted Origin, {ctype or 'no Content-Type'} -> {st}")
    st, out, _ = raw("POST", "/speak", body,
                     {"Origin": ALLOWED, "Content-Type": "application/json; charset=utf-8"})
    check("A3", st == 200, f"allowlisted Origin, application/json; charset=utf-8 -> {st}")
    settle()
    st, out, _ = raw("POST", "/stop", headers={"Origin": ALLOWED})
    check("A3", st == 200, f"allowlisted Origin, bodyless POST /stop -> {st}")
    n = len(enq)
    st, out, _ = raw("POST", "/speak", body, {"Origin": FOREIGN, "Content-Type": "text/plain"})
    check("A3", st == 403 and envelope(out) and "Origin" in out["error"] and len(enq) == n,
          f"foreign Origin, text/plain -> {st} (Origin check first)")

    # ---- A4 output_file ------------------------------------------------------
    os.makedirs(out_dir, exist_ok=True)
    link = os.path.join(out_dir, "link")
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(outside, link)
    n = len(enq)
    escapes = ["../escape.wav", "../../escape.wav",
               os.path.join(outside, "abs.wav"), "link/through.wav",
               out_dir]   # scratch-only targets: a broken tree must not write outside scratch
    for bad in escapes:
        st, out, _ = post("/speak", {"text": "File hello", "output_file": bad})
        check("A4", st == 400 and envelope(out) and len(enq) == n,
              f"output_file {bad!r} -> {st}")
    settle()
    leaked = [p for p in (os.path.join(cache, "gnome-speaks", "escape.wav"),
                          os.path.join(cache, "escape.wav"))
              if os.path.exists(p)] + os.listdir(outside)
    check("A4", not writes and not leaked, f"nothing written (writes={writes}, leaked={leaked})")
    st, out, _ = post("/speak", {"text": "Saved hello", "output_file": "saved.wav"})
    want = os.path.join(os.path.realpath(out_dir), "saved.wav")
    wrote = wait_for(lambda: writes == [want] and os.path.exists(want), 4.0)
    check("A4", st == 200 and out.get("output_file") == want and wrote,
          f"plain name -> {st} {out.get('output_file')!r}, written={wrote} writes={writes}")
    st, out, _ = post("/speak", {"text": "Abs hello", "output_file": os.path.join(out_dir, "abs-in.wav")})
    check("A4", st == 200, f"absolute path inside the directory -> {st}")
    settle()

    # ---- A5 /cast gate -------------------------------------------------------
    harness.isolation.pretend_extension(mod, present=False)
    st, out, _ = post("/cast", {"text": "cast echo"})
    check("A5", st == 503 and envelope(out) and "extension" in out["error"] and not casts,
          f"master switch off -> {st} {out}, spellbook consulted={len(casts)}")
    harness.isolation.pretend_extension(mod, present=True)

    st, out, _ = post("/cast", {"text": "cast echo"})
    check("A5", st == 200 and out.get("ok") is True and casts == ["cast echo"],
          f"master switch on -> {st} {out} (positive control)")
    settle()

    # Status quo (2026-09-12, pending JP): quiet hours and call mute do not
    # refuse /cast, exactly as they do not refuse a spoken cast.
    now = dt.datetime.now()
    mod.CONFIG.update({"quiet_hours": True,
                       "quiet_hours_start": (now - dt.timedelta(hours=1)).strftime("%H:%M"),
                       "quiet_hours_end": (now + dt.timedelta(hours=1)).strftime("%H:%M")})
    svc._quiet_override = None
    assert svc.quiet_hours_active()
    st, out, _ = post("/cast", {"text": "cast echo"})
    check("A5", st == 200 and len(casts) == 2,
          f"quiet hours stay exempt -> {st} {out}")
    mod.CONFIG["quiet_hours"] = False
    settle()

    # _call_mute_reason() is the verdict the watcher feeds (the call-mute
    # suite measures the watcher itself); /cast must not read it.
    svc._call_mute_reason = lambda: "on a call (repro holds the microphone) -- speech is muted"
    st, out, _ = post("/cast", {"text": "cast echo"})
    check("A5", st == 200 and len(casts) == 3,
          f"call mute stays exempt -> {st} {out}")
    del svc._call_mute_reason
    settle()

    # ---- A6 version ----------------------------------------------------------
    st, out, _ = raw("GET", "/api/version")
    hostname = socket.gethostname()
    check("A6", st == 200 and "host" not in out and hostname not in json.dumps(out),
          f"/api/version keys={sorted(out)}")

    srv.shutdown()
    shutil.rmtree(cache, ignore_errors=True)
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s): {sorted(set(FAILS))}")
        return 1
    print("PASS: loopback agents admitted unchanged; foreign origins, forged hosts, "
          "non-JSON browser POSTs and escaping output files refused; /cast behind "
          "the master switch; no hostname")
    return 0


if __name__ == "__main__":
    sys.exit(main())
