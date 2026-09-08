#!/usr/bin/env python3
"""Issue #132: POST /speak {interrupt:true} must not cancel the user's dictation.

_handle_speak's interrupt branch called stop(drain_queue=False), and stop()
runs cancel_all() -- EVERY live token, including a begun stt-stream token with
state=listening. Measured: an agent's interrupt:true drove a live dictation to
idle and discarded the transcript. The normal queue path holds agent speech
while the mic is open (_queue_hold_reason); interrupt was the one agent path
that inverted "user outranks agents".

Streaming is the DEFAULT STT mode, so the fakes below are the ones from
repro_c: the path JP's dictation actually takes.

E1 -- interrupt during a live dictation: the session survives (state stays
      listening, its token is not cancelled, the response says `held`), the
      hotkey still delivers the transcript, and the interrupting item is spoken
      AFTER the user is done -- held, not dropped.
E2 -- interrupt while an AGENT item is playing: it is still cut off
      (outcome 'interrupted'). E2 is the regression guard: an interrupt with no
      user session in flight must keep its "flush everything and speak now"
      meaning.

exit 0 = both hold; exit 1 = at least one is violated.
"""
import http.client
import http.server
import json
import sys
import threading
import time

import harness

PHRASE = "the quick brown fox"


class FakeStdout:
    def __init__(self, frame_bytes):
        self._frame = b"\x00" * frame_bytes

    def read(self, n):
        time.sleep(0.005)
        return self._frame


class FakeProc:
    """Stands in for the pw-record/arecord subprocess."""

    def __init__(self, frame_bytes):
        self.stdout = FakeStdout(frame_bytes)
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def poll(self):
        return 0 if self.terminated else None

    def wait(self, timeout=None):
        return 0


class FakeWS:
    """Stands in for the Azure streaming WebSocket."""

    def __init__(self):
        self.closed = threading.Event()

    def settimeout(self, t):
        pass

    def send(self, payload, opcode=None):
        if self.closed.is_set():
            raise RuntimeError("ws closed")

    def recv(self):
        if self.closed.is_set():
            raise RuntimeError("ws closed")
        time.sleep(0.05)
        return "MSG"


def serve(mod, svc):
    """A real SpeechHTTPHandler on an ephemeral port -- never 7710."""
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

    return srv, post


def make_check(failures):
    def check(ok, label):
        print(f"    {'OK  ' if ok else 'FAIL'}: {label}")
        if not ok:
            failures.append(label)
    return check


def case_e1():
    """Interrupt lands while a streaming dictation has a phrase in hand."""
    mod, events, inj = harness.load(fake_tts_seconds=0.3)

    proc = FakeProc(mod.FRAME_BYTES)
    ws = FakeWS()
    phrase_seen = threading.Event()

    mod.HAS_WS = True
    mod.HAS_VAD = False
    mod._take_prewarmed_rec = lambda *a, **k: proc
    mod._get_stt_ws = lambda *a, **k: (ws, True)
    mod._init_stt_ws_session = lambda *a, **k: None
    mod._make_ws_audio_msg = lambda *a, **k: b""
    mod.calibrate_noise = lambda p: (1000.0, [])
    mod.rms_energy = lambda chunk: 5000.0
    mod.is_speech_energy = lambda chunk, vad, thr: True
    mod._rest_stt_fallback = lambda *a, **k: ""

    def fake_parse(msg, phrases, partial_holder, end_word_event, end_word,
                   _log, raw_partial_holder=None, use_lexical=False):
        if not phrase_seen.is_set():
            phrases.append(PHRASE)
            partial_holder[0] = PHRASE
            if raw_partial_holder is not None:
                raw_partial_holder[0] = PHRASE
            phrase_seen.set()
        return "hypothesis"

    mod._parse_ws_msg = fake_parse

    svc = harness.make_service(mod)
    svc._stt_mode = "streaming"
    inj.available = lambda: False
    srv, post = serve(mod, svc)

    failures = []
    check = make_check(failures)

    try:
        assert svc.start_listening() == "ok", "setup: start_listening refused"
        assert phrase_seen.wait(5.0), "setup: no phrase was ever recognised"
        time.sleep(0.1)
        live_before = svc._cancels.live()
        assert live_before, "setup: no live token while listening"

        status, body = post("/speak", {"text": "AGENT barging in",
                                       "source": "agent", "interrupt": True})
        check(status == 200 and body.get("ok") is True,
              f"POST /speak interrupt answered 200 ({status} {body})")
        check(body.get("held") == "listening",
              f"response reports held='listening' (got {body.get('held')!r})")
        check(body.get("state") == "queued",
              f"response state is 'queued', not 'speaking' ({body.get('state')!r})")
        time.sleep(0.2)
        check(svc.current_state == "listening",
              f"dictation still listening (state {svc.current_state!r})")
        check(all(not t.cancelled for t in live_before),
              "the dictation's token was NOT cancelled")
        check(not events, f"agent speech did not start over the open mic ({events})")

        # The hotkey ends the utterance; the transcript must still land.
        svc.stop_listening()
        ws.closed.set()
        harness.wait_for(lambda: inj.all_text(), 4.0)
        texts = [t for t, _ in inj.all_text()]
        check(PHRASE in texts, f"transcript reached the cursor ({texts})")

        # ...and the interrupting item is spoken once the user is done.
        spoken = lambda: any(e[0] == "start" and e[1] == "AGENT" for e in events)
        harness.wait_for(spoken, 5.0)
        check(spoken(), f"held agent item was spoken afterwards ({events})")
        harness.wait_for(lambda: not svc._cancels.live(), 5.0)
        check(not svc._cancels.live(),
              f"no token leaked ({[repr(t) for t in svc._cancels.live()]})")
    finally:
        srv.shutdown()
    return failures


def case_e2():
    """Regression guard: interrupt still cuts off a PLAYING agent item."""
    mod, events, inj = harness.load(fake_tts_seconds=2.0)
    svc = harness.make_service(mod)
    srv, post = serve(mod, svc)
    failures = []
    check = make_check(failures)

    try:
        first = svc.enqueue_speech("FIRST long agent item", source="a")[0]
        svc.enqueue_speech("SECOND queued agent item", source="a")
        harness.wait_for(lambda: any(e[0] == "start" and e[1] == "FIRST"
                                     for e in events), 5.0)
        status, body = post("/speak", {"text": "URGENT agent item",
                                       "source": "b", "interrupt": True})
        check(status == 200 and body.get("ok") is True,
              f"POST /speak interrupt answered 200 ({status} {body})")
        check("held" not in body, f"no `held` with no user session ({body})")
        check(body.get("flushed") == 1,
              f"flushed the queued item ({body.get('flushed')})")
        harness.wait_for(lambda: harness.outcome_of(svc, first) is not None, 5.0)
        check(harness.outcome_of(svc, first) == "interrupted",
              f"playing item was cut off (outcome {harness.outcome_of(svc, first)!r})")
        spoken = lambda: any(e[0] == "start" and e[1] == "URGENT" for e in events)
        harness.wait_for(spoken, 5.0)
        check(spoken(), f"interrupting item was spoken ({events})")
        harness.wait_for(lambda: not svc._cancels.live()
                         and svc._queue_current is None, 5.0)
        svc.stop()
    finally:
        srv.shutdown()
    return failures


def case_e3():
    """Interrupt while IDLE must speak its own item.

    Found while writing E1: the dispatcher parks in get() holding a snapshot
    of _drain_gen taken BEFORE the interrupt's drain, so the item enqueued
    right after the drain was dropped as 'drained mid-claim'. Measured 10/10
    on main -- the response said "speaking" and nothing was ever spoken.
    """
    mod, events, inj = harness.load(fake_tts_seconds=0.05)
    svc = harness.make_service(mod)
    srv, post = serve(mod, svc)
    failures = []
    check = make_check(failures)
    try:
        dropped = 0
        for n in range(5):
            tag = f"T{n}"
            status, body = post("/speak", {"text": f"{tag} idle interrupt",
                                           "source": "a", "interrupt": True})
            assert status == 200, f"setup: POST /speak -> {status}"
            harness.wait_for(
                lambda: harness.outcome_of(svc, body["id"]) is not None, 3.0)
            if harness.outcome_of(svc, body["id"]) != "done":
                dropped += 1
            time.sleep(0.13)   # land at varied offsets inside get(timeout=0.2)
        check(dropped == 0,
              f"every idle interrupt was spoken ({dropped}/5 dropped as canceled)")
        harness.wait_for(lambda: not svc._cancels.live()
                         and svc._queue_current is None, 5.0)
    finally:
        srv.shutdown()
    return failures


def main():
    print(f"service under test: {harness.SVC_PATH}")
    print("\n  E1: interrupt:true during a live dictation -- the user's session survives")
    f1 = case_e1()
    print("\n  E2: interrupt:true over agent speech -- still cuts it off")
    f2 = case_e2()
    print("\n  E3: interrupt:true while idle -- speaks its own item")
    f3 = case_e3()
    print()
    if f1 or f2 or f3:
        why = []
        if f1:
            why.append("interrupt:true preempts the user (E1)")
        if f2:
            why.append("interrupt lost its agent-cutoff meaning (E2)")
        if f3:
            why.append("idle interrupt drops its own item (E3)")
        print(f"FAIL: {len(f1) + len(f2) + len(f3)} check(s) -- {' / '.join(why)}")
        return 1
    print("PASS: interrupt:true holds behind the user, cuts off agents, "
          "and is spoken when idle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
