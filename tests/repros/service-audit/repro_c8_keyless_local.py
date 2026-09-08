#!/usr/bin/env python3
"""repro: a keyless local-first install (key=None, speech_backend=local) is not
refused at the front door with "Azure Speech key not configured" (#125).

Before: six copy-pasted `if not CONFIG.get("key")` gates (start_listening x2,
speak, talk, POST /speak -> 503, main) refused every call outright, while the
STT/TTS paths underneath had complete Wyoming routes that never touch Azure.
After: ONE `_speech_ready()` helper owns the verdict and names what is missing.

  K1  key=None + wyoming_host + speech_backend=local: ready (None)
  K2  key=None, no wyoming_host: refused, message names the local server too
  K3  key=None + wyoming_host, speech_backend=azure: refused, points at
      speech_backend=local
  K4  key=None + local, LAN server on cooldown: refused ("cooldown", "Azure");
      cooldown lifted -> ready again
  K5  Talk (full-duplex, Azure-only): refused even keyless-local, says why
  K6  POST /speak on a port-0 server, keyless-local: 200 and TTS actually runs
  K7  POST /speak, key=None, no wyoming_host: 503 and the error names the
      local server, not just the key
  K8  D-Bus speak(): True keyless-local, TTS runs
  K9  start_listening(): "ok" keyless-local (streaming rerouted to the batch
      path, which carries the Wyoming route)
  K10 the literal "Azure Speech key not configured" lives ONLY in _speech_ready
  K11 speech_tts.tts() with key=None + prefer_local reaches Wyoming and never
      starts an Azure player -- the hedge the issue carried ("that the Wyoming
      paths complete with key=None is inferred, not measured"), measured with
      the LAN socket stubbed
  K12 stt._rest_stt_fallback() with key=None + prefer_local returns Wyoming's
      text without an Azure request

Run:  GS_SVC_PATH=<gnome-speaks-service.py> python3 repro_c8_keyless_local.py
Exit 0 = all hold; 1 = a verdict failed; 2 = setup failure.
"""
import http.client
import http.server
import importlib.util
import inspect
import json
import os
import sys
import threading
import time

import harness

# Pinned BEFORE load(): the harness writes these into the scratch config.json
# too, so _reload_config_flags() re-reading it mid-run is a no-op (#90).
harness.CONFIG_PINS["key"] = None
harness.CONFIG_PINS["wyoming_host"] = "127.0.0.1"
harness.CONFIG_PINS["speech_backend"] = "local"
os.environ.pop("SPEECH_FORCE_OFFLINE", None)

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} ({label}) {detail}")
    if not cond:
        FAILS.append(label)


def set_cfg(mod, **kw):
    """Change CONFIG and the scratch config.json together -- a bare CONFIG[...]
    write to a _SYNC_FLAGS key (speech_backend is one) is undone by the next
    non-quick start_listening()."""
    mod.CONFIG.update(kw)
    with open(mod.CONFIG_PATH, encoding="utf-8") as f:
        on_disk = json.load(f)
    on_disk.update(kw)
    with open(mod.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(on_disk, f)


def load_real_speech_tts(mod):
    """A fresh copy of speech_tts whose tts() is the REAL one (the harness
    replaced the shared module's tts with a fake). It imports the same
    `state`/`wyoming` module objects, so CONFIG and the breaker are shared."""
    src = mod.speech_tts.__file__
    spec = importlib.util.spec_from_file_location("speech_tts_real", src)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    return real


def helper_verdicts(mod, wy, svc):
    check("K1", mod._speech_ready() is None, f"keyless local: {mod._speech_ready()!r}")

    set_cfg(mod, wyoming_host="")
    msg = mod._speech_ready() or ""
    check("K2", "wyoming_host" in msg or "local speech server" in msg, repr(msg))
    set_cfg(mod, wyoming_host="127.0.0.1")

    set_cfg(mod, speech_backend="azure")
    msg = mod._speech_ready() or ""
    check("K3", "speech_backend=local" in msg, repr(msg))
    set_cfg(mod, speech_backend="local")

    wy.mark_local_down(cooldown=60)
    msg = mod._speech_ready() or ""
    down_refused = "cooldown" in msg.lower() and "azure" in msg.lower()
    wy.mark_local_up()
    check("K4", down_refused and mod._speech_ready() is None,
          f"on cooldown: {msg!r}; lifted: {mod._speech_ready()!r}")

    # -- K5: Talk has no local path and must say so -------------------------
    msg = mod._speech_ready(need_azure=True) or ""
    ret = svc.talk("hello")
    check("K5", "Talk" in msg and isinstance(ret, str) and ret.startswith("error:"),
          f"helper: {msg!r}; talk(): {ret!r}")

    # -- K10: one owner of the words ----------------------------------------
    with open(harness.SVC_PATH, encoding="utf-8") as f:
        src = f.read()
    literal = "Azure Speech key not configured"
    total = src.count(literal)
    in_helper = inspect.getsource(mod._speech_ready).count(literal)
    check("K10", total == in_helper and in_helper >= 1,
          f"{total} occurrence(s) in the service, {in_helper} inside _speech_ready")


def main():
    if not os.path.isfile(harness.SVC_PATH):
        print(f"!! SETUP FAILURE: no such file: {harness.SVC_PATH}")
        return 2
    mod, events = harness.load(fake_tts_seconds=0.05)
    # Pre-fix trees have no helper. Its verdict checks (K1-K5, K10) then fail by
    # construction, but the BEHAVIOUR checks (K6-K9, K11-K12) still run, so a
    # baseline run shows what the door actually did -- 503, False, "error:".
    has_helper = hasattr(mod, "_speech_ready")
    wy = mod.wyoming_mod
    if not hasattr(wy, "prefer_local"):
        print("!! SETUP FAILURE: speech-to-cli has no wyoming.prefer_local (needs #104)")
        return 2
    wy.mark_azure_up()
    wy.mark_local_up()
    svc = harness.make_service(mod)
    time.sleep(0.2)

    # -- K1..K5, K10: the helper's verdicts ----------------------------------
    if has_helper:
        helper_verdicts(mod, wy, svc)
    else:
        check("K1", False, "service has no _speech_ready() -- the six key gates are still copy-pasted")

    # -- K6/K7: POST /speak over real HTTP on an ephemeral port -------------
    mod.SpeechHTTPHandler.service = svc
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), mod.SpeechHTTPHandler)
    port = srv.server_address[1]
    assert port != 7710
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def post(path, payload):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", path, body=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        out = json.loads(r.read())
        c.close()
        return r.status, out

    del events[:]
    st, body = post("/speak", {"text": "K6 keyless local", "source": "repro"})
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not any(k == "end" and t == "K6" for k, t, _ in events):
        time.sleep(0.02)
    spoken = any(k == "end" and t == "K6" for k, t, _ in events)
    check("K6", st == 200 and spoken, f"status={st} body={body} tts_ran={spoken}")

    set_cfg(mod, wyoming_host="")
    st, body = post("/speak", {"text": "K7 nothing to speak with", "source": "repro"})
    err = str(body.get("error", ""))
    check("K7", st == 503 and ("wyoming_host" in err or "local speech server" in err),
          f"status={st} error={err!r}")
    set_cfg(mod, wyoming_host="127.0.0.1")
    srv.shutdown()

    # -- K8: D-Bus speak() ---------------------------------------------------
    del events[:]
    ok = svc.speak("K8 direct")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not any(k == "end" and t == "K8" for k, t, _ in events):
        time.sleep(0.02)
    check("K8", ok is True and any(k == "end" and t == "K8" for k, t, _ in events),
          f"speak()={ok!r} events={[(k, t) for k, t, _ in events]}")
    deadline = time.monotonic() + 3.0
    while svc.current_state != "idle" and time.monotonic() < deadline:
        time.sleep(0.02)

    # -- K9: start_listening() opens (batch path; the worker is a no-op) ----
    svc._batch_stt_worker = lambda mode, token: None
    ret = svc.start_listening()
    check("K9", ret == "ok", f"start_listening()={ret!r} state={svc.current_state}")
    svc.stop()
    time.sleep(0.1)

    # -- K11: the TTS route completes with key=None (network stubbed) -------
    real = load_real_speech_tts(mod)
    wy.mark_azure_up(); wy.mark_local_up()
    azure_player = []
    real._tts_wyoming = lambda text, proc, **kw: {"spoken": True, "engine": "wyoming"}
    real._take_prewarmed_player = lambda rate: azure_player.append("prewarm") or None
    real._start_player = lambda rate: azure_player.append("start") or None
    try:
        result = real.tts("K11 keyless")
        k11 = result.get("spoken") is True and result.get("engine") == "wyoming" and not azure_player
        detail = f"result={result} azure_player={azure_player}"
    except Exception as exc:  # a KeyError on CONFIG["key"] would land here
        k11, detail = False, f"raised {exc!r}"
    check("K11", k11, detail)

    # -- K12: the batch-STT route completes with key=None (network stubbed) --
    stt = sys.modules["stt"]   # the service does `from stt import ...`; the module itself is here
    azure_calls = []

    class _NoAzure:
        def post(self, *a, **kw):
            azure_calls.append(a)
            raise AssertionError("Azure REST was attempted with no key")

    saved = (stt._wyoming_stt, stt.get_http_session)
    stt._wyoming_stt = lambda raw, _log: "hello from the lan"
    stt.get_http_session = lambda: _NoAzure()
    try:
        text = stt._rest_stt_fallback(b"\x00" * 3200)
        k12 = text == "hello from the lan" and not azure_calls
        detail = f"text={text!r} azure_calls={len(azure_calls)}"
    except Exception as exc:
        k12, detail = False, f"raised {exc!r}"
    finally:
        stt._wyoming_stt, stt.get_http_session = saved
    check("K12", k12, detail)

    if FAILS:
        print(f"FAIL: {len(FAILS)} verdict(s) -- keyless local-first is refused or misreported: {FAILS}")
        return 1
    print("PASS: a keyless local-first install speaks and listens; refusals name what is actually missing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
