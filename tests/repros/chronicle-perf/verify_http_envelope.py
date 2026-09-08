"""#128: every HTTP request gets a JSON answer, even the ones that blow up.

Before the fix, do_GET/do_POST dispatched into _handle_* with no try/except,
so an escaping exception made socketserver print a traceback and close the
socket with NO response -- curl reported "Empty reply from server" and an
agent had nothing to parse. Four inputs reached that path from the outside:

  - `Content-Length: abc`          int() sat outside _read_json_body's try
  - a JSON body that is a list     body.get -> AttributeError
  - {"text": 5}                    .strip() -> AttributeError
  - any non-queue.Full raise inside enqueue_speech (forced here)

Bound on port 0 like verify_endpoints.py -- never 7710, the live service
owns that. Exit 0 = every case answered JSON with the right status; 1 = at
least one still drops the connection or answers the wrong code.
"""
import http.client
import http.server
import json
import threading

import harness

mod, events = harness.load(0.05)
svc = harness.make_service(mod)
fails = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{'' if ok else '  <- ' + detail}")
    if not ok:
        fails.append(name)


mod.SpeechHTTPHandler.service = svc
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), mod.SpeechHTTPHandler)
port = srv.server_address[1]
assert port != 7710
threading.Thread(target=srv.serve_forever, daemon=True).start()


def raw_post(path, body, headers):
    """Returns (status, parsed_json) or (None, repr(exc)) on a dropped socket.

    Headers are passed through verbatim so a malformed Content-Length reaches
    the server instead of being corrected by http.client.
    """
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.putrequest("POST", path, skip_accept_encoding=True)
        for k, v in headers.items():
            c.putheader(k, v)
        c.endheaders()
        if body:
            c.send(body)
        r = c.getresponse()
        data = r.read()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, {"_raw": data[:80].decode("latin-1")}
    except (http.client.HTTPException, OSError) as exc:
        return None, repr(exc)
    finally:
        c.close()


def post(path, payload):
    body = json.dumps(payload).encode()
    return raw_post(path, body, {"Content-Type": "application/json",
                                 "Content-Length": str(len(body))})


def envelope(name, result, status):
    st, body = result
    check(f"{name} -> {status}", st == status, f"got {st} {body}")
    check(f"{name} is a JSON error envelope",
          isinstance(body, dict) and body.get("ok") is False
          and isinstance(body.get("error"), str), f"{body}")


# ---- 400s: malformed input must be rejected, not crashed on ----------
envelope("Content-Length: abc",
         raw_post("/speak", b"", {"Content-Type": "application/json",
                                  "Content-Length": "abc"}), 400)
envelope("JSON list body", post("/speak", ["hello"]), 400)
envelope("JSON string body", post("/skip", "hello"), 400)
envelope('{"text": 5} on /speak', post("/speak", {"text": 5}), 400)
envelope('{"text": 5} on /cast', post("/cast", {"text": 5}), 400)
envelope('{"text": null} on /speak', post("/speak", {"text": None}), 400)

# ---- 500: a handler that raises still answers -------------------------
def boom(*a, **k):
    raise RuntimeError("forced by verify_http_envelope")


real_enqueue = svc.enqueue_speech
svc.enqueue_speech = boom
try:
    envelope("enqueue_speech raising RuntimeError",
             post("/speak", {"text": "hello"}), 500)
finally:
    svc.enqueue_speech = real_enqueue

# GET side goes through the same envelope
real_status = mod.SpeechHTTPHandler._handle_status
mod.SpeechHTTPHandler._handle_status = boom
try:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request("GET", "/status")
        r = c.getresponse()
        res = (r.status, json.loads(r.read()))
    except (http.client.HTTPException, OSError, ValueError) as exc:
        res = (None, repr(exc))
    finally:
        c.close()
    envelope("GET /status handler raising", res, 500)
finally:
    mod.SpeechHTTPHandler._handle_status = real_status

# ---- control: the healthy path is untouched ---------------------------
st, body = post("/speak", {"text": "control utterance"})
check("healthy POST /speak still 200", st == 200 and body.get("ok") is True,
      f"{st} {body}")
st, body = post("/skip", {})
check("bodyless-equivalent POST /skip still 200", st == 200, f"{st} {body}")
svc._drain_tts_queue()
svc.stop(drain_queue=False)
srv.shutdown()

print()
if fails:
    print("FAILURES:", ", ".join(fails))
    raise SystemExit(1)
print("all checks passed")
