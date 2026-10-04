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
  A5  POST /cast is an agent seam (JP, 2026-10-04): master switch off,
      quiet hours, call mute -> 503 each, and the spellbook is never
      consulted; with all three open it casts (positive control)
      A5b: the master switch is reported first (switch off + quiet hours
      -> the extension words); a SPOKEN cast (_try_cast, which both STT
      workers call) is not gated by quiet hours or call mute
  A6  GET /api/version carries no hostname
  A7  duplicates: two Origin headers (allowlisted + foreign) -> 403; two
      Host headers (loopback + foreign) -> 403
  A8  allowlist parsing drops "null" and "*" in any case/spacing/trailing
      slash and keeps the rest; an Origin: null request is refused even when
      the raw setting listed it
  A9  output directory: a pre-existing 0755 out/ is tightened to 0700 on
      first use; a symlinked out/ -> 503 envelope and nothing written
      through it; a symlinked PARENT ($XDG_CACHE_HOME a link) is allowed

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


def stat_mode(path):
    import stat as _stat
    return _stat.S_IMODE(os.lstat(path).st_mode)


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
        pairs = list(headers.items()) if isinstance(headers, dict) else list(headers or [])
        if body is not None and not any(k.lower() == "content-length" for k, _ in pairs):
            pairs.append(("Content-Length", str(len(body))))
        for k, v in pairs:
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

    now = dt.datetime.now()
    mod.CONFIG.update({"quiet_hours": True,
                       "quiet_hours_start": (now - dt.timedelta(hours=1)).strftime("%H:%M"),
                       "quiet_hours_end": (now + dt.timedelta(hours=1)).strftime("%H:%M")})
    svc._quiet_override = None
    assert svc.quiet_hours_active()
    st, out, _ = post("/cast", {"text": "cast quiet hours"})
    check("A5", st == 503 and "quiet hours" in out.get("error", "") and not casts,
          f"quiet hours -> {st} {out} (incl. the quiet-hours toggle spell)")
    mod.CONFIG["quiet_hours"] = False

    # _call_mute_reason() is the verdict the watcher feeds (the call-mute
    # suite measures the watcher itself).
    svc._call_mute_reason = lambda: "on a call (repro holds the microphone) -- speech is muted"
    st, out, _ = post("/cast", {"text": "cast echo"})
    check("A5", st == 503 and "on a call" in out.get("error", "") and not casts,
          f"call mute -> {st} {out}")
    del svc._call_mute_reason

    st, out, _ = post("/cast", {"text": "cast echo"})
    check("A5", st == 200 and out.get("ok") is True and casts == ["cast echo"],
          f"all gates open -> {st} {out} (positive control)")
    settle()

    # A5b: master switch first; spoken casts unchanged.
    harness.isolation.pretend_extension(mod, present=False)
    mod.CONFIG["quiet_hours"] = True
    svc._quiet_override = None
    st, out, _ = post("/cast", {"text": "cast echo"})
    check("A5", st == 503 and "extension" in out.get("error", "") and len(casts) == 1,
          f"switch off + quiet hours -> the master switch is named first ({out.get('error', '')[:40]!r})")
    harness.isolation.pretend_extension(mod, present=True)
    svc._call_mute_reason = lambda: "on a call (repro holds the microphone) -- speech is muted"
    spoken = svc._try_cast("cast echo")
    check("A5", spoken is True and len(casts) == 2,
          "a spoken cast (_try_cast) still runs in quiet hours and on a call")
    del svc._call_mute_reason
    mod.CONFIG["quiet_hours"] = False
    settle()

    # ---- A7 duplicate headers ------------------------------------------------
    n = len(enq)
    st, out, _ = raw("POST", "/speak", json.dumps({"text": "Dup hello"}).encode(),
                     [("Content-Type", "application/json"),
                      ("Origin", ALLOWED), ("Origin", FOREIGN)])
    dup_enq = [a for a, _k in enq[n:] if a and a[0] == "Dup hello"]
    check("A7", st == 403 and envelope(out) and not dup_enq,
          f"two Origin headers (allowlisted, foreign) -> {st}, enqueued={len(dup_enq)}")
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest("GET", "/status", skip_host=True, skip_accept_encoding=True)
    c.putheader("Host", f"127.0.0.1:{port}")
    c.putheader("Host", "evil.example")
    c.endheaders()
    r = c.getresponse(); dup_out = json.loads(r.read() or b"null"); c.close()
    check("A7", r.status == 403 and envelope(dup_out),
          f"two Host headers (loopback, foreign) -> {r.status}")

    # ---- A8 allowlist parsing ------------------------------------------------
    parsed = mod._parse_origin_allowlist(
        'null, NULL,  null , Null/, *, */,  *  , http://ok.example/, HTTP://Two.Example')
    check("A8", parsed == frozenset({"http://ok.example", "http://two.example"}),
          f"null/* variants dropped, real entries kept: {sorted(parsed)}")
    saved = mod.HTTP_ALLOWED_ORIGINS
    mod.HTTP_ALLOWED_ORIGINS = mod._parse_origin_allowlist("null *")
    st, _, h = raw("GET", "/status", headers={"Origin": "null"})
    check("A8", st == 403 and "access-control-allow-origin" not in h,
          f"setting listed null -> Origin: null still {st}")
    mod.HTTP_ALLOWED_ORIGINS = saved

    # ---- A9 output directory -------------------------------------------------
    def fresh_cache(name):
        c_ = os.path.join(harness.SCRATCH, name)
        shutil.rmtree(c_, ignore_errors=True)
        os.makedirs(os.path.join(c_, "gnome-speaks"))
        os.environ["XDG_CACHE_HOME"] = c_
        return c_

    c9 = fresh_cache("cache-loose")
    loose = os.path.join(c9, "gnome-speaks", "out")
    os.mkdir(loose)
    os.chmod(loose, 0o755)
    st, out, _ = post("/speak", {"text": "Tight hello", "output_file": "t.wav"})
    mode = stat_mode(loose)
    check("A9", st == 200 and mode == 0o700, f"pre-existing 0755 out/ -> {st}, mode {oct(mode)}")
    settle()

    c9 = fresh_cache("cache-linked")
    target = os.path.join(harness.SCRATCH, "link-target")
    shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target)
    os.chmod(target, 0o755)
    os.symlink(target, os.path.join(c9, "gnome-speaks", "out"))
    n_w = len(writes)
    st, out, _ = post("/speak", {"text": "Linked hello", "output_file": "l.wav"})
    settle()
    check("A9", st == 503 and envelope(out) and len(writes) == n_w
          and not os.listdir(target) and stat_mode(target) == 0o755,
          f"symlinked out/ -> {st} {out}, target untouched (mode {oct(stat_mode(target))})")

    real_cache = os.path.join(harness.SCRATCH, "cache-real")
    shutil.rmtree(real_cache, ignore_errors=True)
    os.makedirs(real_cache)
    link_cache = os.path.join(harness.SCRATCH, "cache-parent-link")
    if os.path.lexists(link_cache):
        os.remove(link_cache)
    os.symlink(real_cache, link_cache)
    os.environ["XDG_CACHE_HOME"] = link_cache
    st, out, _ = post("/speak", {"text": "Parent hello", "output_file": "p.wav"})
    want = os.path.join(real_cache, "gnome-speaks", "out", "p.wav")
    check("A9", st == 200 and out.get("output_file") == want and wait_for(lambda: os.path.exists(want), 4.0),
          f"symlinked parent ($XDG_CACHE_HOME) -> {st} {out.get('output_file')!r}")
    settle()
    for d_ in ("cache-loose", "cache-linked", "link-target", "cache-real"):
        shutil.rmtree(os.path.join(harness.SCRATCH, d_), ignore_errors=True)
    os.remove(link_cache)
    os.environ["XDG_CACHE_HOME"] = cache

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
          "non-JSON browser POSTs and escaping output files refused; /cast gated "
          "like /speak; no hostname")
    return 0


if __name__ == "__main__":
    sys.exit(main())
