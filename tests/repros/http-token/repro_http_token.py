#!/usr/bin/env python3
"""HTTP API per-install token (#186, phase 2).

  T1  token file: first run creates $XDG_CONFIG_HOME/gnome-speaks/token 0600 in
      a 0700 dir, a valid token, and curl.conf (0600) carrying the same token;
      a second load returns the SAME token; a loose 0644 token file and a 0755
      dir are tightened; a corrupt token file is replaced; a symlinked token
      file is refused (None) and its target neither read-modified nor chmod'ed;
      a racing first run (link() loses) adopts the winner's token
  T2  grace (GNOME_SPEAKS_REQUIRE_TOKEN off, the default): no token -> 200 and
      counted in GET /status auth.tokenless; a WRONG token -> 401 with nothing
      enqueued; the right token as Bearer and as X-Speaks-Token -> 200
  T3  required: no token -> 401 + WWW-Authenticate on EVERY gated endpoint
      (GET /status /queue /chronicle /api/version /voices, POST /speak /stop
      /skip /pause /resume /cast /respeak) with no side effect; wrong -> 401;
      right -> 200; GET /health with no token -> 200 and only {"ok": true};
      malformed (Basic, empty Bearer, two credentials) -> 401
  T4  required + no usable token on the server -> 401 (fails closed), /health 200
  T5  admission still comes first: a foreign Origin on /health -> 403; an
      allowlisted CORS preflight needs no token (204) and its
      Access-Control-Allow-Headers names X-Speaks-Token and Authorization
  T6  the token never reaches a log record (wrong- and right-token requests,
      the creation path); the comparison is hmac.compare_digest

Hermetic: an ephemeral port (asserted != 7710), no live service, scratch in the
repo's gitignored tmp/ keyed by PID; XDG_CONFIG_HOME is pointed into scratch
before anything reads it. exit 0 = all hold; 1 = a violation.
"""
import http.client
import http.server
import inspect
import json
import logging
import os
import re
import stat
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "cancel-tokens"))
os.environ.pop("GNOME_SPEAKS_REQUIRE_TOKEN", None)
import harness  # noqa: E402

CONFIG_HOME = os.path.join(harness.SCRATCH, "config")
os.environ["XDG_CONFIG_HOME"] = CONFIG_HOME

FAILS = []
ALLOWED = "http://allowed.example:8080"
FOREIGN = "http://evil.example"


def check(label, ok, msg):
    print(f"    {'ok  ' if ok else 'FAIL'}: {label} {msg}")
    if not ok:
        FAILS.append(label)


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            self.lines.append(str(record.msg))


def main():
    print(f"service under test: {harness.SVC_PATH}")
    mod, _events, _inj = harness.load(fake_tts_seconds=0.05)
    if not hasattr(mod, "_load_or_create_token"):
        print("FAIL: this tree has no per-install token (_load_or_create_token)")
        return 1
    cap = Capture()
    logging.getLogger().addHandler(cap)
    mod.log.addHandler(cap)

    tdir = os.path.join(CONFIG_HOME, "gnome-speaks")
    tpath = os.path.join(tdir, "token")
    cpath = os.path.join(tdir, "curl.conf")
    check("T1", mod._token_path() == tpath, f"token path in scratch: {mod._token_path()}")

    # ---- T1 token file --------------------------------------------------------
    os.umask(0o002)
    tok = mod._load_or_create_token()
    check("T1", isinstance(tok, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,256}", tok or "") is not None,
          f"first run made a valid token (len={len(tok or '')})")
    check("T1", os.path.isfile(tpath) and mode(tpath) == 0o600 and mode(tdir) == 0o700,
          f"token {oct(mode(tpath))} in dir {oct(mode(tdir))}")
    with open(tpath) as f:
        check("T1", f.read().strip() == tok, "file holds the returned token")
    with open(cpath) as f:
        conf = f.read()
    check("T1", conf == 'header = "X-Speaks-Token: %s"\n' % tok and mode(cpath) == 0o600,
          f"curl.conf carries the token, mode {oct(mode(cpath))}")
    check("T1", mod._load_or_create_token() == tok, "second load returns the same token")
    os.chmod(tdir, 0o755)
    check("T1", mod._load_or_create_token() == tok and mode(tdir) == 0o700,
          f"a loose 0755 dir is tightened and the 0600 token kept ({oct(mode(tdir))})")
    os.chmod(tpath, 0o644)
    t_exp = mod._load_or_create_token()
    check("T1", t_exp and t_exp != tok and mode(tpath) == 0o600,
          f"a group/world-readable token is ROTATED, not just chmod'ed ({oct(mode(tpath))})")
    tok = t_exp
    # short writes: os.write may write less than asked; nothing truncated is published
    real_os_write = os.write
    os.write = lambda fd, data: real_os_write(fd, bytes(data[:5]))
    try:
        sw = os.path.join(harness.SCRATCH, "short-write")
        mod._write_private_file(sw, b"0123456789abcdefghij" * 3 + b"\n")
    finally:
        os.write = real_os_write
    with open(sw, "rb") as f:
        check("T1", f.read() == b"0123456789abcdefghij" * 3 + b"\n",
              "a 5-bytes-per-call os.write still publishes the whole token")
    with open(tpath, "w") as f:
        f.write("short\n")
    t2 = mod._load_or_create_token()
    check("T1", t2 and t2 != tok and mode(tpath) == 0o600,
          "a corrupt token file is replaced with a fresh token")
    with open(cpath) as f:
        check("T1", t2 in f.read(), "curl.conf follows the new token")
    tok = t2
    # racing first run: the exclusive publish loses, the loser adopts the winner's
    os.remove(tpath)
    real_write = mod._write_private_file

    def racing_write(path, data, exclusive=False):
        if exclusive and path == tpath:
            real_write(path, b"winnerTOKEN_" + b"w" * 40 + b"\n")   # the other process
            raise FileExistsError(path)
        return real_write(path, data, exclusive)
    mod._write_private_file = racing_write
    t3 = mod._load_or_create_token()
    mod._write_private_file = real_write
    check("T1", t3 == "winnerTOKEN_" + "w" * 40, f"a lost first-run race adopts the winner ({t3!r:.20})")
    tok = t3
    leftovers = [n for n in os.listdir(tdir) if n.endswith(".tmp")]
    check("T1", not leftovers, f"no temp files left ({leftovers})")
    # symlinked token file: refused, target untouched
    victim = os.path.join(harness.SCRATCH, "victim-token")
    with open(victim, "w") as f:
        f.write("V" * 43 + "\n")
    os.chmod(victim, 0o644)
    os.remove(tpath)
    os.symlink(victim, tpath)
    t4 = mod._load_or_create_token()
    with open(victim) as f:
        vbody = f.read()
    check("T1", t4 is None and mode(victim) == 0o644 and vbody == "V" * 43 + "\n" and os.path.islink(tpath),
          f"symlinked token file -> None ({t4!r}), target untouched ({oct(mode(victim))})")
    os.remove(tpath)
    tok = mod._load_or_create_token()
    check("T1", bool(tok), "a fresh token once the link is gone")

    # ---- server ---------------------------------------------------------------
    mod.HTTP_TOKEN = tok
    mod.HTTP_REQUIRE_TOKEN = False
    mod.HTTP_ALLOWED_ORIGINS = mod._parse_origin_allowlist(ALLOWED)
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
    stops = []
    real_stop = svc.stop

    def spy_stop(*a, **k):
        stops.append(1)
        return real_stop(*a, **k)
    svc.stop = spy_stop
    # /voices must never reach Azure from a repro.
    mod.speech_tts.get_voices = lambda: [{"ShortName": "repro-voice"}]

    def raw(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.putrequest(method, path, skip_accept_encoding=True)
        pairs = list(headers.items()) if isinstance(headers, dict) else list(headers or [])
        if body is not None:
            pairs.append(("Content-Type", "application/json"))
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

    def post(path, payload, headers=None):
        return raw("POST", path, json.dumps(payload).encode(), headers)

    def settle():
        wait_for(lambda: svc._tts_queue.qsize() == 0 and svc.current_state == "idle", 4.0)

    bearer = {"Authorization": "Bearer " + tok}
    xtok = {"X-Speaks-Token": tok}
    wrong = {"X-Speaks-Token": "W" * 43}

    # ---- T2 grace ---------------------------------------------------------------
    before = mod.SpeechHTTPHandler.auth_info()["tokenless"]
    st, out, _ = post("/speak", {"text": "Tokenless hello", "source": "repro"})
    check("T2", st == 200 and len(enq) == 1, f"grace: no token -> {st}")
    settle()
    st, out, _ = raw("GET", "/status")
    auth = (out or {}).get("auth", {})
    check("T2", st == 200 and auth.get("required") is False and auth.get("token") is True
          and auth.get("tokenless", 0) >= before + 2 and "/status" in (auth.get("tokenless_last") or {}).get("request", ""),
          f"grace: /status auth={auth}")
    n = len(enq)
    st, out, h = post("/speak", {"text": "Wrong hello"}, wrong)
    check("T2", st == 401 and len(enq) == n and "www-authenticate" in h,
          f"grace: WRONG token -> {st}, enqueued={len(enq) - n}")
    for name, hdr in (("Bearer", bearer), ("X-Speaks-Token", xtok)):
        st, out, _ = post("/speak", {"text": f"Right hello {name}"}, hdr)
        check("T2", st == 200 and out.get("ok") is True, f"grace: right token via {name} -> {st}")
        settle()

    # ---- T3 required ------------------------------------------------------------
    mod.HTTP_REQUIRE_TOKEN = True
    gated = [("GET", "/status", None), ("GET", "/queue", None), ("GET", "/chronicle", None),
             ("GET", "/api/version", None), ("GET", "/voices", None),
             ("POST", "/speak", {"text": "Required hello"}), ("POST", "/stop", None),
             ("POST", "/skip", {}), ("POST", "/pause", None), ("POST", "/resume", None),
             ("POST", "/cast", {"text": "cast echo"}), ("POST", "/respeak", {})]
    n, nc, ns = len(enq), len(casts), len(stops)
    for method, path, payload in gated:
        body = json.dumps(payload).encode() if payload is not None else None
        st, out, h = raw(method, path, body)
        ok = (st == 401 and isinstance(out, dict) and out.get("ok") is False
              and h.get("www-authenticate", "").startswith("Bearer"))
        check("T3", ok, f"required: no token {method} {path} -> {st}")
    check("T3", len(enq) == n and len(casts) == nc and len(stops) == ns,
          f"required: no side effect (enqueued={len(enq) - n} casts={len(casts) - nc} stops={len(stops) - ns})")
    st, out, _ = raw("GET", "/status", headers=wrong)
    check("T3", st == 401, f"required: wrong token -> {st}")
    st, out, _ = raw("GET", "/status", headers=xtok)
    check("T3", st == 200 and out.get("auth", {}).get("required") is True, f"required: right token /status -> {st}")
    st, out, _ = post("/speak", {"text": "Required right hello"}, bearer)
    check("T3", st == 200 and len(enq) == n + 1, f"required: right Bearer /speak -> {st}")
    settle()
    st, out, _ = post("/cast", {"text": "cast echo"}, xtok)
    check("T3", st == 200 and casts[-1:] == ["cast echo"], f"required: right token /cast -> {st}")
    settle()
    st, out, h = raw("GET", "/health")
    check("T3", st == 200 and out == {"ok": True}, f"required: /health with no token -> {st} {out}")
    st, out, _ = raw("GET", "/health?verbose=1")
    check("T3", st == 200 and out == {"ok": True}, "required: /health ignores a query and adds nothing")
    st, out, _ = raw("POST", "/health")
    check("T3", st == 401, f"required: POST /health is not exempt -> {st}")
    for label, hdrs in (("Basic", {"Authorization": "Basic " + tok}),
                        ("empty Bearer", {"Authorization": "Bearer "}),
                        ("two credentials", [("Authorization", "Bearer " + tok), ("X-Speaks-Token", tok)]),
                        ("two X-Speaks-Token", [("X-Speaks-Token", tok), ("X-Speaks-Token", tok)])):
        st, out, _ = raw("GET", "/status", headers=hdrs)
        check("T3", st == 401, f"required: malformed ({label}) -> {st}")
    st, out, _ = raw("GET", "/status", headers={"X-Speaks-Token": tok[:-1]})
    check("T3", st == 401, f"required: a one-character-short token -> {st}")

    # ---- T4 required + no server token -------------------------------------------
    mod.HTTP_TOKEN = None
    st, out, _ = raw("GET", "/status")
    check("T4", st == 401, f"required, no server token, no client token -> {st}")
    st, out, _ = raw("GET", "/status", headers=xtok)
    check("T4", st == 401, f"required, no server token, a client token -> {st} (fails closed)")
    st, out, _ = raw("GET", "/health")
    check("T4", st == 200, f"/health still answers -> {st}")
    mod.HTTP_TOKEN = tok

    # ---- T5 admission order + CORS -------------------------------------------------
    st, out, _ = raw("GET", "/health", headers={"Origin": FOREIGN})
    check("T5", st == 403, f"foreign Origin on /health -> {st} (admission first)")
    st, out, h = raw("OPTIONS", "/speak", headers={"Origin": ALLOWED, "Access-Control-Request-Method": "POST",
                                                  "Access-Control-Request-Headers": "x-speaks-token"})
    allow = h.get("access-control-allow-headers", "")
    check("T5", st == 204 and "X-Speaks-Token" in allow and "Authorization" in allow,
          f"allowlisted preflight without a token -> {st}, allow-headers={allow!r}")
    st, out, _ = raw("GET", "/status", headers={"Origin": ALLOWED})
    check("T5", st == 401, f"allowlisted Origin, no token, required -> {st}")
    st, out, h = raw("GET", "/status", headers={"Origin": ALLOWED, **xtok})
    check("T5", st == 200 and h.get("access-control-allow-origin") == ALLOWED,
          f"allowlisted Origin + token -> {st}")

    # ---- T6 secrecy ---------------------------------------------------------------
    leaked = [ln for ln in cap.lines if tok in ln or "W" * 43 in ln]
    check("T6", not leaked, f"no log record carries a token ({len(cap.lines)} records scanned)")
    src = inspect.getsource(mod.SpeechHTTPHandler._token_matches)
    check("T6", "compare_digest" in src and "==" not in src.split('"""')[-1],
          "the comparison is hmac.compare_digest")

    # ---- T7 the documented recipe, with the real curl -----------------------------
    import shutil
    import subprocess
    if shutil.which("curl"):
        # curl.conf on disk holds the token of the LAST load; the server must match it.
        mod.HTTP_TOKEN = mod._load_or_create_token()
        url = f"http://127.0.0.1:{port}/status"
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                            "-K", cpath, url], capture_output=True, text=True, timeout=20)
        check("T7", r.stdout == "200", f"curl -K curl.conf /status (required) -> {r.stdout}")
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", url],
                           capture_output=True, text=True, timeout=20)
        check("T7", r.stdout == "401", f"plain curl /status (required) -> {r.stdout}")
    else:
        print("    skip: T7 (no curl)")

    srv.shutdown()
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s): {sorted(set(FAILS))}")
        return 1
    print("PASS: per-install token created private and stable; tokenless callers admitted "
          "and counted in grace, refused when required; wrong/malformed tokens refused; "
          "/health open; nothing logged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
