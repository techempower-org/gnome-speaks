#!/usr/bin/env python3
"""Repros for PR #70 / issue #49 — the streaming-STT -> Wyoming handoff.

Four cases, all driven through the REAL _streaming_stt_cycle with a REAL
subprocess recorder on a REAL pipe (so pipe back-pressure is measured, not
modelled) and the REAL speech-to-cli _rest_stt_fallback / breaker:

  A  stop_listening() already requested when the WS connect gives up
     -> the whole utterance recorded so far must still reach the recognizer
  B  speech spoken during a long (10 s-class) failed connect
     -> must reach the recognizer with no dropout
  C  breaker open (Azure marked down) -> next press must not touch the WS
  D  Azure healthy -> the WS still receives the complete frame stream

Every case asserts on the CAPTURE TIMELINE recovered from the audio itself:
fake_rec.py stamps sample[0] of each frame with its capture index, so a gap
in the delivered indices is proof that audio was lost, not an inference.

Usage:  GS_SVC_PATH=<service.py> python3 repro_offline_handoff.py [A B C D]

ENV CONTRACT: GS_SVC_PATH is the only input -- the service.py under test. The
worktree dir is derived from its dirname by the shared harness, so sibling
modules always come from the same tree.

BASELINE IS A PINNED SHA, NEVER A BRANCH. Against a moving ref this suite goes
vacuous the moment its fix merges -- it starts comparing the fix to itself and
passes forever:

    GS_BASELINE_REF=79594dd     # last commit BEFORE #70 merged (as 7eaeb02)
    git archive $GS_BASELINE_REF | tar -x -C <dir>
    GS_SVC_PATH=<dir>/gnome-speaks-service.py python3 repro_offline_handoff.py

Case I additionally needs #72 (merged as 848b855), so against 79594dd it fails
for two independent reasons -- the missing Wyoming handoff AND the swallowed
dead recorder. Verified 2026-09-06.

STATE: owned entirely by the shared harness, which pins $XDG_STATE_HOME to a
per-PID scratch dir BEFORE importing the service (CHRONICLE_PATH is computed at
module import from it) and asserts the module's paths resolve inside it. This
file must NOT set XDG_STATE_HOME itself: a fixed path here is shared between
concurrent agents, and that is what made the chronicle suite read another
process's writes as a service regression.
"""
import os
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT = os.path.join(os.path.dirname(HERE), "service-audit")
sys.path.insert(0, AUDIT)
sys.path.insert(0, HERE)

# XDG_STATE_HOME is deliberately NOT set here -- see STATE in the docstring.
os.environ["SPEECH_FORCE_OFFLINE"] = "1"     # breaker forced: Wyoming direct

import harness  # noqa: E402

FRAME_BYTES = 960
FRAME_MS = 30


# --------------------------------------------------------------------------
# instrumentation
# --------------------------------------------------------------------------

def capture_indices(pcm):
    """Recover the capture-frame index stamped in sample[0] of each frame."""
    out = []
    for off in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
        out.append(struct.unpack_from('<h', pcm, off)[0])
    return out


def gaps(idx):
    """[(after, before)] for every discontinuity > 1 in the capture timeline."""
    return [(a, b) for a, b in zip(idx, idx[1:]) if b - a > 1]


class FakeWS:
    """Enough of websocket.WebSocket for _init_stt_ws_session/_parse_ws_msg."""

    def __init__(self, script=()):
        self.audio = bytearray()
        self._inbox = list(script)
        self._lock = threading.Lock()
        self.closed = False

    def send(self, data, opcode=None):
        if isinstance(data, (bytes, bytearray)):
            body = bytes(data)
            hlen = struct.unpack_from('>H', body, 0)[0]
            payload = body[2 + hlen:]
            if payload[:4] == b"RIFF":
                return                      # the WAV header stt.py prepends
            with self._lock:
                self.audio.extend(payload)

    def recv(self):
        import websocket
        with self._lock:
            if self._inbox:
                return self._inbox.pop(0)
        raise websocket.WebSocketTimeoutException("no message")

    def settimeout(self, _t):
        pass

    def close(self):
        self.closed = True


def phrase_msg(text):
    return ('Path: speech.phrase\r\nX-RequestId: x\r\n\r\n'
            '{"RecognitionStatus":"Success","DisplayText":"%s",'
            '"NBest":[{"Display":"%s"}]}' % (text, text))


def build(speech_ms=4000, exit_ms=0):
    """Load the service with every dangerous seam replaced, recorder = fake_rec."""
    mod, _ = harness.load()
    svc = harness.make_service(mod)

    rec_env = dict(os.environ, FAKE_REC_SPEECH_MS=str(speech_ms),
                   FAKE_REC_EXIT_MS=str(exit_ms))
    mod._build_rec_cmd = lambda *a, **k: [sys.executable, "-u",
                                          os.path.join(HERE, "fake_rec.py")]
    mod._take_prewarmed_rec = lambda *a, **k: None
    mod._invalidate_stt_ws = lambda *a, **k: None
    mod.subprocess_env = rec_env

    # Popen without the env kwarg would lose FAKE_REC_SPEECH_MS.
    real_popen = mod.subprocess.Popen

    def popen(cmd, **kw):
        kw.setdefault("env", rec_env)
        return real_popen(cmd, **kw)
    mod.subprocess.Popen = popen

    # Never type anything, never speak, never touch the real injector.
    typed = []
    class _Inj:
        def available(self): return False
        def supports_preedit(self): return False
        def commit(self, text): typed.append(text)
        def finalize(self, text): typed.append(text)
        def replace_text(self, *a): pass
        def send_backspaces(self, *a): pass
        def type_text(self, *a): pass
        def paste(self, text): typed.append(text)
        def end(self): pass
    mod.get_injector = lambda: _Inj()

    # GLib.idle_add only QUEUES; with no main loop nothing runs, so record
    # the calls instead of executing them (same as the real service here).
    idle_calls = []
    real_idle_add = mod.GLib.idle_add

    def rec_idle_add(fn, *args, **kw):
        idle_calls.append((getattr(fn, "__name__", str(fn)), args))
        return 0
    mod.GLib.idle_add = rec_idle_add
    svc._idle_calls = idle_calls
    svc._errors = [] 
    svc._save_config_flag = lambda *a, **k: None    # never touch config.json
    # Harnesses share ONE CONFIG dict and _reload_config_flags() re-reads JP's
    # LIVE config into it -- a scenario that calls start_listening() would
    # otherwise measure his desktop, not this code. Pin it per scenario.
    svc._reload_config_flags = lambda *a, **k: None
    svc._try_cast = lambda *a, **k: False
    svc._wake_gate_blocks = lambda *a, **k: False
    svc._idle_after_stt = lambda *a, **k: svc._set_state("idle")

    # The recognizer seam: capture exactly what bytes reach offline STT.
    seen = {"pcm": b"", "calls": 0}

    def fake_transcribe(host, port, pcm, **kw):
        seen["calls"] += 1
        seen["pcm"] = bytes(pcm)
        return "offline transcript"
    mod.wyoming_mod.transcribe = fake_transcribe
    import stt as stt_mod
    stt_mod.wyoming.transcribe = fake_transcribe

    def _no_azure(*a, **k):                          # hard guard: no WAN calls
        raise AssertionError("repro tried to reach Azure REST")
    stt_mod.get_http_session = _no_azure

    mod.CONFIG["wyoming_host"] = "wyoming.invalid"   # in-process only
    mod.CONFIG["dictation_mode"] = True
    mod.state._cached_noise_threshold = None         # deterministic calibration
    return mod, svc, seen, typed


def run_cycle(mod, svc, timeout=40):
    tok = svc._cancels.issue("stt-stream")
    t = threading.Thread(target=svc._streaming_stt_cycle, args=(tok,), daemon=True)
    t.start()
    return tok, t


def result(name, ok, detail):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return ok


# --------------------------------------------------------------------------
# A — stop already requested when the connect gives up
# --------------------------------------------------------------------------

def case_a():
    """stop_listening() during a pending connect must NOT discard the words.

    A stop request means 'finish this utterance', not 'throw it away'; only
    stop() (which cancels the token) discards.
    """
    mod, svc, seen, typed = build(speech_ms=4000)
    CONNECT_S, STOP_AT_S = 1.6, 1.0

    def slow_fail():
        time.sleep(CONNECT_S)
        raise OSError("simulated Azure unreachable")
    mod._get_stt_ws = slow_fail

    t_start = time.monotonic()
    tok, th = run_cycle(mod, svc)
    time.sleep(STOP_AT_S)
    svc._stop_event.set()                 # hotkey: end the utterance, KEEP text
    th.join(timeout=30)

    idx = capture_indices(seen["pcm"])
    expect = int(CONNECT_S * 1000 / FRAME_MS)
    ok = (seen["calls"] == 1 and len(idx) >= expect * 0.8
          and not gaps(idx) and typed == ["offline transcript"])
    return result(
        "A stop-pending-at-handoff",
        ok,
        f"{len(seen['pcm'])} B / {len(idx)} frames reached the recognizer "
        f"(expected >= {int(expect * 0.8)} = {CONNECT_S:.1f}s of capture); "
        f"gaps={gaps(idx)}; typed={typed}; elapsed={time.monotonic()-t_start:.1f}s")


# --------------------------------------------------------------------------
# B — speech during a long failed connect
# --------------------------------------------------------------------------

def case_b():
    """A 10 s connect timeout must not cost the words spoken during it.

    The pipe holds 65536 B = 2.048 s; anything the recorder produced after
    that is lost unless something drains the pipe while the connect is
    pending.
    """
    mod, svc, seen, typed = build(speech_ms=4000)
    CONNECT_S = 6.0

    def slow_fail():
        time.sleep(CONNECT_S)
        raise OSError("simulated Azure unreachable")
    mod._get_stt_ws = slow_fail

    tok, th = run_cycle(mod, svc)
    time.sleep(CONNECT_S + 0.2)
    svc._stop_event.set()                 # end the utterance right at handoff
    th.join(timeout=30)

    idx = capture_indices(seen["pcm"])
    pipe_frames = 65536 // FRAME_BYTES    # 68 — what the pipe alone can hold
    expect = int(CONNECT_S * 1000 / FRAME_MS)
    ok = (len(idx) >= expect * 0.8 and not gaps(idx))
    return result(
        "B speech-during-failed-connect",
        ok,
        f"{len(idx)} frames delivered (pipe alone holds {pipe_frames}; "
        f"the utterance is {expect}); gaps={gaps(idx)}; "
        f"span={idx[0] if idx else '-'}..{idx[-1] if idx else '-'}")


# --------------------------------------------------------------------------
# C — breaker open: the next press must not wait on the WS
# --------------------------------------------------------------------------

def case_c():
    """Breaker semantics: once Azure is marked down, the NEXT press pays no
    connect timeout -- it is routed straight to the offline batch path.

    SPEECH_FORCE_OFFLINE is removed here so the only thing that can route the
    session offline is mark_azure_down() + a configured Wyoming host.
    """
    forced = os.environ.pop("SPEECH_FORCE_OFFLINE", None)
    try:
        return _case_c_body()
    finally:
        if forced is not None:
            os.environ["SPEECH_FORCE_OFFLINE"] = forced
        mod_reset = None


def _case_c_body():
    mod, svc, seen, typed = build(speech_ms=500)
    calls = {"ws": 0}

    def counted():
        calls["ws"] += 1
        time.sleep(10.0)
        raise OSError("simulated Azure unreachable")
    mod._get_stt_ws = counted
    mod.stt_dispatch = lambda **kw: {"text": "batch offline transcript"}
    mod.wyoming_mod.mark_azure_up()
    assert not mod.wyoming_mod.skip_azure(), "breaker must start closed"
    mod.wyoming_mod.mark_azure_down()
    assert mod.wyoming_mod.skip_azure(), "mark_azure_down must trip skip_azure"

    svc._set_state("idle")
    t0 = time.monotonic()
    r = svc.start_listening()
    if getattr(svc, "_stt_thread", None):
        svc._stt_thread.join(timeout=10)
    elapsed = time.monotonic() - t0

    ok = (r == "ok" and calls["ws"] == 0 and elapsed < 2.0
          and typed == ["batch offline transcript"])
    return result(
        "C breaker-open-skips-ws",
        ok,
        f"start_listening={r}; _get_stt_ws calls={calls['ws']} (must be 0); "
        f"elapsed={elapsed:.2f}s; typed={typed}")


# --------------------------------------------------------------------------
# D — Azure healthy: the WS still gets the whole stream
# --------------------------------------------------------------------------

def case_d():
    mod, svc, seen, typed = build(speech_ms=1500)
    ws = FakeWS(script=[phrase_msg("hello from azure")])
    mod._get_stt_ws = lambda: (ws, True)

    tok, th = run_cycle(mod, svc)
    time.sleep(2.5)
    svc._stop_event.set()
    th.join(timeout=30)

    idx = capture_indices(bytes(ws.audio))
    ok = (len(idx) >= 60 and not gaps(idx) and seen["calls"] == 0
          and typed == ["hello from azure"])
    return result(
        "D azure-healthy-unchanged",
        ok,
        f"{len(idx)} frames reached the WS; gaps={gaps(idx)}; "
        f"offline recognizer calls={seen['calls']} (must be 0); typed={typed}")


# --------------------------------------------------------------------------
# E — the other ordering: stop arrives DURING the offline capture
# --------------------------------------------------------------------------

def case_e():
    """stop_listening() after the handoff ends the capture and keeps the text.

    Same invariant as A, opposite ordering: here stopping() goes true while
    the offline VAD capture is already running.
    """
    mod, svc, seen, typed = build(speech_ms=4000)
    CONNECT_S, STOP_AT_S = 1.0, 2.6

    def slow_fail():
        time.sleep(CONNECT_S)
        raise OSError("simulated Azure unreachable")
    mod._get_stt_ws = slow_fail

    tok, th = run_cycle(mod, svc)
    time.sleep(STOP_AT_S)
    svc._stop_event.set()                 # hotkey mid-capture: KEEP the text
    th.join(timeout=30)

    idx = capture_indices(seen["pcm"])
    expect = int(STOP_AT_S * 1000 / FRAME_MS)
    ok = (len(idx) >= expect * 0.8 and not gaps(idx)
          and typed == ["offline transcript"])
    return result(
        "E stop-during-offline-capture",
        ok,
        f"{len(idx)} frames delivered (utterance so far is {expect}); "
        f"gaps={gaps(idx)}; typed={typed}")


# --------------------------------------------------------------------------
# F — stop() still means ABANDON
# --------------------------------------------------------------------------

def case_f():
    """The counterpart invariant: a cancelled token discards the transcript.

    Nothing above may be bought by typing text the user asked to abandon.
    """
    mod, svc, seen, typed = build(speech_ms=4000)

    def slow_fail():
        time.sleep(1.0)
        raise OSError("simulated Azure unreachable")
    mod._get_stt_ws = slow_fail

    tok, th = run_cycle(mod, svc)
    time.sleep(1.8)
    svc._cancels.cancel_all()             # stop(): ABANDON the utterance
    svc._stop_event.set()
    th.join(timeout=30)

    ok = (typed == [])
    return result(
        "F stop-abandons-transcript",
        ok,
        f"typed={typed} (must be empty); recognizer calls={seen['calls']}")


# --------------------------------------------------------------------------
# G — no Wyoming host: the 4-attempt WS budget is unchanged
# --------------------------------------------------------------------------

def case_g():
    """Without an offline recognizer there is nowhere better to go, so the
    original retry budget stays. Guards the one-strike rule from leaking
    into installs that have no Wyoming server."""
    forced = os.environ.pop("SPEECH_FORCE_OFFLINE", None)
    try:
        mod, svc, seen, typed = build(speech_ms=500)
        mod.CONFIG["wyoming_host"] = ""          # in-process only
        calls = {"ws": 0}

        def counted():
            calls["ws"] += 1
            raise OSError("simulated Azure unreachable")
        mod._get_stt_ws = counted
        import stt as stt_mod
        real_fallback = stt_mod._rest_stt_fallback
        stt_mod._rest_stt_fallback = lambda *a, **k: ""
        mod._rest_stt_fallback = lambda *a, **k: ""
        try:
            tok, th = run_cycle(mod, svc)
            time.sleep(1.0)
            svc._stop_event.set()
            th.join(timeout=30)
        finally:
            stt_mod._rest_stt_fallback = real_fallback

        ok = calls["ws"] == 4
        return result("G no-wyoming-keeps-4-attempts", ok,
                      f"_get_stt_ws attempts={calls['ws']} (must be 4)")
    finally:
        if forced is not None:
            os.environ["SPEECH_FORCE_OFFLINE"] = forced


# --------------------------------------------------------------------------
# H — a recovered utterance must not also raise "STT WebSocket failed"
# --------------------------------------------------------------------------

def case_h():
    """The user sees ONE outcome, not a contradiction.

    Recovered offline -> no error notification (the log carries it).
    Recovered nothing -> the connect failure IS the outcome, so it is raised.
    """
    mod, svc, seen, typed = build(speech_ms=1500)

    def fail():
        time.sleep(0.8)
        raise OSError("simulated Azure unreachable")
    mod._get_stt_ws = fail

    tok, th = run_cycle(mod, svc)
    time.sleep(1.2)
    svc._stop_event.set()
    th.join(timeout=30)
    recovered_errors = [a[0] for n, a in svc._idle_calls if n == "_emit_error"]

    # Same session, but the offline recognizer returns nothing.
    mod2, svc2, seen2, typed2 = build(speech_ms=1500)
    mod2._get_stt_ws = fail
    import stt as stt_mod
    stt_mod.wyoming.transcribe = lambda *a, **k: ""
    mod2.wyoming_mod.transcribe = lambda *a, **k: ""
    tok2, th2 = run_cycle(mod2, svc2)
    time.sleep(1.2)
    svc2._stop_event.set()
    th2.join(timeout=30)
    silent_errors = [a[0] for n, a in svc2._idle_calls if n == "_emit_error"]

    ok = (typed == ["offline transcript"] and recovered_errors == []
          and typed2 == [] and len(silent_errors) == 1
          and "WebSocket failed" in silent_errors[0])
    return result(
        "H no-false-error-when-recovered", ok,
        f"recovered: typed={typed} errors={recovered_errors}; "
        f"unrecovered: typed={typed2} errors={silent_errors}")


# --------------------------------------------------------------------------
# I — mic yanked AND Azure down: the words survive, ONE actionable toast
# --------------------------------------------------------------------------

def case_i():
    """The compound failure. #57/#48 says a lost mic is reported once and in
    addition to the words; #49 adds an exit that never used to deliver words
    at all. The lost mic outranks "STT WebSocket failed" -- it is the
    actionable one, and two toasts bury it."""
    mod, svc, seen, typed = build(speech_ms=4000, exit_ms=600)

    def slow_fail():
        time.sleep(1.0)
        raise OSError("simulated Azure unreachable")
    mod._get_stt_ws = slow_fail

    tok, th = run_cycle(mod, svc)
    th.join(timeout=30)

    errors = [a[0] for n, a in svc._idle_calls if n == "_emit_error"]
    idx = capture_indices(seen["pcm"])
    ok = (typed == ["offline transcript"] and len(idx) >= 15
          and len(errors) == 1 and "Microphone disconnected" in errors[0])
    return result(
        "I mic-yanked-and-azure-down", ok,
        f"{len(idx)} frames recovered from the dead recorder; typed={typed}; "
        f"errors={errors} (want exactly one, the microphone one)")


CASES = {"A": case_a, "B": case_b, "C": case_c, "D": case_d,
         "E": case_e, "F": case_f, "G": case_g, "H": case_h, "I": case_i}

if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    want = [a.upper() for a in sys.argv[1:]] or list(CASES)
    ok = True
    for name in want:
        try:
            ok &= bool(CASES[name]())
        except Exception as exc:
            import traceback
            traceback.print_exc()
            ok = False
            print(f"[FAIL] {name}: raised {exc!r}")
    print("\nALL GREEN" if ok else "\nRED")
    sys.exit(0 if ok else 1)
