#!/usr/bin/env python3
"""Issue #21, streaming path: /stop still types the transcript collected so far.

Streaming is the DEFAULT STT mode (stt_mode=auto picks it whenever
websocket-client and webrtcvad are present), so this is the path JP's
dictation actually takes.

_streaming_stt_worker has ONE stop signal, self._stop_event, and two callers
set it for opposite reasons:

  * stop_listening()  -- "end the utterance, KEEP the text"  (the dictation
    hotkey; the docstring says so).  The transcript must be typed.
  * stop()            -- the panic stop: D-Bus Stop, POST /stop, "cast stop",
    and every user-speech preemption.  The transcript must be ABANDONED.

The worker cannot tell them apart.  After the receive loop exits, control
falls straight through to

        user_text = " ".join(phrases).strip()
        ...
        self._set_state("processing")
        ...
        get_injector().commit(user_text)

with no cancellation check anywhere in between.  So a panic stop types
whatever Azure had already returned -- no race, no hang, no un-cancel needed.
(state.cancel_active() does raise the global wire, but nothing on this path
reads it, which is the same missing-authority defect as consequence (b).)

C1 -- stop():          nothing may reach the cursor.
C2 -- stop_listening(): the transcript MUST still reach the cursor.
      C2 is the regression guard: the fix must not break dictation.

exit 0 = both hold; exit 1 = at least one is violated.
"""
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


def run(stop_kind):
    mod, _events, inj = harness.load()

    frame_bytes = mod.FRAME_BYTES
    proc = FakeProc(frame_bytes)
    ws = FakeWS()
    phrase_seen = threading.Event()

    mod.HAS_WS = True
    mod.HAS_VAD = False            # skip webrtcvad; energy gate is stubbed below
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
        return "hypothesis"        # keep the receive loop running

    mod._parse_ws_msg = fake_parse

    svc = harness.make_service(mod)
    svc._stt_mode = "streaming"
    # Route the transcript through the single final commit rather than the
    # live-typing path, so "did it reach the cursor" is one unambiguous fact.
    inj.available = lambda: False

    assert svc.start_listening() == "ok", "setup: start_listening refused"
    assert phrase_seen.wait(5.0), "setup: no phrase was ever recognised"
    time.sleep(0.1)

    t_stop = time.monotonic()
    if stop_kind == "stop":
        svc.stop()
    else:
        svc.stop_listening()
    ws.closed.set()

    harness.wait_for(lambda: inj.all_text(), 4.0)
    time.sleep(0.3)
    # The streaming worker has four exits; none of them may leak its token.
    leaked = getattr(svc, "_cancels", None) and svc._cancels.live()
    if leaked:
        print(f"    FAIL: leaked cancel token(s) {[repr(t) for t in leaked]}")
    return ([(txt, ts - t_stop) for txt, ts in inj.all_text()],
            svc.current_state, bool(leaked))


def main():
    print(f"service under test: {harness.SVC_PATH}")
    failures = 0

    print("\n  C1: stop() -- the panic stop; the transcript must be abandoned")
    typed, st, leaked = run("stop")
    failures += int(leaked)
    if typed:
        failures += 1
        for txt, dt in typed:
            print(f"    FAIL: {txt!r} typed {dt:+.1f}s relative to stop()")
    else:
        print("    OK: nothing reached the cursor")
    print(f"    (state after: {st!r})")

    print("\n  C2: stop_listening() -- the dictation hotkey; text must be KEPT")
    typed, st, leaked = run("stop_listening")
    failures += int(leaked)
    if not typed:
        failures += 1
        print("    FAIL: dictation lost its transcript (regression)")
    else:
        for txt, dt in typed:
            print(f"    OK: {txt!r} typed {dt:+.1f}s relative to stop_listening()")
    print(f"    (state after: {st!r})")

    print()
    if failures:
        print(f"FAIL: {failures}/2 -- stop() and stop_listening() are not "
              f"distinguishable to the streaming worker.")
        return 1
    print("PASS: stop() abandons the transcript, stop_listening() keeps it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
