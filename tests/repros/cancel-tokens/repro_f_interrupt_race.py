#!/usr/bin/env python3
"""PR #146 review (#132): the interrupt branch must never be ABLE to reach a
user token -- not merely decide not to.

repro_e's E1 proved interrupt:true no longer kills a dictation that is already
listening. The review found the gate it added was a snapshot: `held` was read
from current_state, and when it read idle the branch still ran
stop(drain_queue=False) = cancel_all() + _stop_event.set() + _set_state("idle").
A start_listening() landing between that read and cancel_all() had its
stt-stream token cancelled and its state forced idle -- the reviewer measured
21/300 at a 0-4 ms offset, 0/80 serialized.

The fix replaces stop() on this path with a targeted cancel of the queue's
CURRENT item (skip_current(): item and token read together under
_queue_current_lock). The dispatcher only ever publishes tokens it issued
itself, so there is no timing at which an agent seam can cancel a user token.
`held` stays as a response annotation.

F1 -- the race as measured: N iterations of start_listening() vs POST /speak
      {interrupt:true}, the listen aimed at the handler's current_state read
      and swept 0-190 us past it. For every
      iteration in which the dictation began, its token must not be cancelled,
      its _stop_event must not be raised by the agent, and its state must
      still be listening once both calls have returned. Prints the hit count
      so the same harness reads as a control against an unfixed tree.
      Measured at 300 iterations, switch interval 1e-5: 43/126 began
      dictations hit on the pre-revision head fd5c8cd, 51/300 on main
      0e014cc, 0/101 with the fix (57/117 at 1e-6 on fd5c8cd, 0/67 fixed).
      At CPython's default 5 ms switch interval the same harness scored 0/300
      on the unfixed tree -- the aiming thread starved the handler -- so the
      interval is lowered for F1 only (GS_RACE_SWITCH; GS_RACE_ITERS,
      GS_RACE_STEP likewise).
F2 -- the window held open: cancel_all() is wrapped to begin a dictation
      INSIDE it before doing its work, so the read-then-cancel gap is not a
      few microseconds but a certainty. On an unfixed tree this hits 1/1; on
      a fixed tree the interrupt path never calls cancel_all() at all, and the
      dictation started afterwards is untouched. Not timing-dependent, so a
      fast machine cannot turn the defect into a green.

exit 0 = both hold; exit 1 = at least one is violated.
"""
import os
import sys
import threading
import time

import harness
import repro_e_interrupt_vs_dictation as e

ITERS = int(os.environ.get("GS_RACE_ITERS", "300"))
SWITCH_INTERVAL = float(os.environ.get("GS_RACE_SWITCH", "1e-5"))
OFFSET_STEP = float(os.environ.get("GS_RACE_STEP", "1e-5"))
USER_LABELS = ("stt", "stt-stream", "speech", "talk")


def streaming_fakes(mod, holder):
    """repro_c/E1's streaming seams, fed from `holder` so each iteration can
    hand the worker a fresh recorder and WebSocket."""
    mod.HAS_WS = True
    mod.HAS_VAD = False
    mod._take_prewarmed_rec = lambda *a, **k: holder["proc"]
    mod._get_stt_ws = lambda *a, **k: (holder["ws"], True)
    mod._init_stt_ws_session = lambda *a, **k: None
    mod._make_ws_audio_msg = lambda *a, **k: b""
    mod.calibrate_noise = lambda p: (1000.0, [])
    mod.rms_energy = lambda chunk: 5000.0
    mod.is_speech_energy = lambda chunk, vad, thr: True
    mod._rest_stt_fallback = lambda *a, **k: ""
    # Never a phrase: nothing to type, the session just stays open.
    mod._parse_ws_msg = lambda *a, **k: "hypothesis"


def spy_issue(svc, issued):
    real_issue = svc._cancels.issue

    def issue(label):
        token = real_issue(label)
        issued.append(token)
        return token

    svc._cancels.issue = issue


def wind_down(svc, holder):
    """Hotkey semantics (keep the text), then let the fake WS drain out."""
    threading.Timer(0.01, holder["ws"].closed.set).start()
    svc.stop_listening()
    return harness.wait_for(
        lambda: (svc.current_state == "idle"
                 and not svc._cancels.live()
                 and svc._queue_current is None
                 and svc._tts_queue.empty()), 6.0)


def user_hit(svc, issued, listen_result):
    """What the reviewer's race did to a dictation that began."""
    if listen_result != "ok":
        return None
    user_tokens = [t for t in issued if t.label in USER_LABELS]
    if not user_tokens:
        return None
    reasons = []
    if any(t.cancelled for t in user_tokens):
        reasons.append("token cancelled")
    # start_listening() clears _stop_event; only stop()/stop_listening() set it,
    # and neither has been called by the harness yet -- so a raised event here
    # is the agent's stop reaching the user's session.
    if svc._stop_event.is_set():
        reasons.append("_stop_event raised")
    if svc.current_state != "listening":
        reasons.append(f"state {svc.current_state!r}")
    return reasons


def case_f1():
    mod, events, inj = harness.load(fake_tts_seconds=0.02)
    holder = {}
    streaming_fakes(mod, holder)
    svc = harness.make_service(mod)
    svc.__class__ = type("SpiedService", (mod.GnomeSpeaksService,), {})
    svc._stt_mode = "streaming"
    inj.available = lambda: False
    srv, post = e.serve(mod, svc)
    issued = []
    spy_issue(svc, issued)
    failures = []
    check = e.make_check(failures)

    # The window is tens of microseconds wide: it opens at the handler's
    # current_state read and closes at cancel_all(). Two threads with a blind
    # sleep between them almost never land in it (1/134 with 50 us steps from
    # the queue drain -- Event.wait's own wake-up is wider than the window).
    # So the listener is aimed AT the read: a property spy stamps the clock on
    # the handler thread's first current_state read after its drain, and the
    # listener busy-spins on that stamp, then to a swept offset past it. Both
    # spies only observe; the service's own timing is untouched.
    probe = {}
    real_drain = svc._drain_tts_queue

    def drain_spy():
        probe["handler"] = threading.get_ident()
        return real_drain()

    svc._drain_tts_queue = drain_spy
    real_state = mod.GnomeSpeaksService.current_state.fget

    def spy_state(self):
        st = real_state(self)
        if (probe.get("handler") == threading.get_ident()
                and "read" not in probe):
            probe["read"] = time.perf_counter()
        return st

    type(svc).current_state = property(spy_state)

    # Without this the aim is fiction: a Python thread keeps the GIL for the
    # whole switch interval (5 ms) unless it blocks, so the spinning listener
    # STARVES the handler and lands wherever the interpreter happens to switch
    # (measured 0/300 hits on an unfixed tree with the default interval). A
    # fine interval makes the two threads interleave the way two cores would.
    prev_interval = sys.getswitchinterval()
    sys.setswitchinterval(SWITCH_INTERVAL)

    began = 0
    hits = 0
    by_reason = {}
    not_began = {}
    wound = True
    t0 = time.monotonic()
    try:
        for i in range(ITERS):
            holder["proc"] = e.FakeProc(mod.FRAME_BYTES)
            holder["ws"] = e.FakeWS()
            issued.clear()
            probe.clear()
            offset = (i % 20) * OFFSET_STEP   # 0, 10 us, ... 190 us
            result = {}

            def do_post():
                result["post"] = post("/speak", {"text": f"AGENT {i}",
                                                 "source": "agent",
                                                 "interrupt": True})

            def do_listen():
                deadline = time.perf_counter() + 2.0
                while "read" not in probe and time.perf_counter() < deadline:
                    pass
                target = probe.get("read", 0.0) + offset
                while time.perf_counter() < target:
                    pass
                # quick=True is the loop-restart entry: no config re-read
                # before the state check, so the listen lands where aimed.
                result["listen"] = svc.start_listening(quick=True)

            ta = threading.Thread(target=do_post)
            tb = threading.Thread(target=do_listen)
            ta.start()
            tb.start()
            ta.join(5.0)
            tb.join(5.0)
            time.sleep(0.03)                    # let a stop() land, if one did
            reasons = user_hit(svc, issued, result.get("listen"))
            if reasons is not None:
                began += 1
                if reasons:
                    hits += 1
                    for r in reasons:
                        by_reason[r] = by_reason.get(r, 0) + 1
            else:
                # Mostly "busy (speaking)": the interrupting item was claimed
                # by the dispatcher first. Those iterations are the OTHER
                # ordering, not a failure to race -- printed so a low `began`
                # can be read.
                why = str(result.get("listen"))
                not_began[why] = not_began.get(why, 0) + 1
            if not wind_down(svc, holder):
                wound = False
                break
        elapsed = time.monotonic() - t0
        print(f"    {ITERS} iterations in {elapsed:.1f}s: {began} dictations "
              f"began, {hits} hit by the interrupt {dict(by_reason)}")
        print(f"    did not begin: {not_began}")
        check(wound, "every iteration wound down to idle with no live token")
        check(began >= ITERS // 5,
              f"the race was exercised ({began}/{ITERS} dictations began)")
        check(hits == 0,
              f"no dictation was cancelled/stopped/idled by interrupt:true "
              f"({hits}/{began})")
    finally:
        sys.setswitchinterval(prev_interval)
        srv.shutdown()
    return failures, began, hits


def case_f2():
    mod, events, inj = harness.load(fake_tts_seconds=0.02)
    holder = {"proc": e.FakeProc(mod.FRAME_BYTES), "ws": e.FakeWS()}
    streaming_fakes(mod, holder)
    svc = harness.make_service(mod)
    svc._stt_mode = "streaming"
    inj.available = lambda: False
    srv, post = e.serve(mod, svc)
    issued = []
    spy_issue(svc, issued)
    failures = []
    check = e.make_check(failures)

    # Hold the window open: whoever calls cancel_all() now finds a dictation
    # that began after their state read and before their cancel.
    real_cancel_all = svc._cancels.cancel_all
    calls = []

    def cancel_all_with_listener(*a, **kw):
        calls.append(svc.start_listening())
        harness.wait_for(lambda: any(t.label == "stt-stream" for t in issued), 2.0)
        return real_cancel_all(*a, **kw)

    svc._cancels.cancel_all = cancel_all_with_listener
    try:
        status, body = post("/speak", {"text": "AGENT window", "source": "agent",
                                       "interrupt": True})
        check(status == 200 and body.get("ok") is True,
              f"POST /speak interrupt answered 200 ({status} {body})")
        time.sleep(0.05)
        check(not calls,
              f"the interrupt path never called cancel_all() (it did: {calls})")
        if not calls:
            # No dictation was started inside the window; start one now and
            # prove the interrupt that just happened left nothing behind.
            calls.append(svc.start_listening())
        reasons = user_hit(svc, issued, calls[0])
        check(reasons == [],
              f"the dictation that began in/after the window is intact "
              f"(began={calls[0]!r}, {reasons})")
        svc._cancels.cancel_all = real_cancel_all
        check(wind_down(svc, holder),
              "wound down to idle with no live token; held item spoken")
        spoken = any(ev[0] == "start" and ev[1] == "AGENT" for ev in events)
        check(spoken, f"the interrupting item was spoken once the user was done ({events})")
    finally:
        svc._cancels.cancel_all = real_cancel_all
        srv.shutdown()
    return failures


def main():
    print(f"service under test: {harness.SVC_PATH}")
    print(f"\n  F1: start_listening() vs POST /speak interrupt:true, "
          f"{ITERS} iterations aimed 0-190 us past the handler's state read")
    f1, began, hits = case_f1()
    print("\n  F2: cancel_all() held open with a dictation beginning inside it")
    f2 = case_f2()
    print()
    print(f"RESULT: race hits {hits}/{began}; F2 {'clean' if not f2 else 'violated'}")
    if f1 or f2:
        why = []
        if f1:
            why.append(f"interrupt reached a user token in the race ({hits}/{began})")
        if f2:
            why.append("interrupt path can cancel a dictation that begins inside its window")
        print(f"FAIL: {len(f1) + len(f2)} check(s) -- {' / '.join(why)}")
        return 1
    print("PASS: interrupt:true cannot reach a user token at any offset.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
