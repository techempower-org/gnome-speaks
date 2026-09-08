#!/usr/bin/env python3
"""#167: the dispatcher's gate and its state claim must be ONE step.

repro_f's residual red (1 hit per ~120 dictations, ~1 run in 4, reason always
`state 'idle'` -- never a cancelled token, never a raised _stop_event) was not
the interrupt at all. The dispatcher checks _queue_hold_reason() under
_queue_current_lock, publishes the item, then -- outside that lock -- calls
_set_state("speaking"). start_listening() checks `current_state != "idle"` and
later calls _set_state("listening"), with no lock in common. If listening lands
between the gate check and the dispatcher's claim, the agent item plays over
the opening mic (listening -> speaking, forced), and _speak_worker's finally
sees state == speaking with its own _speak_token and sets idle -- while the
dictation's stt-stream token is live and its thread running. Badge idle, mic
open, an agent talking over it. Older than #146; repro_f only made it visible
because every idle-time interrupt enqueues an item exactly as a listen starts.

The fix makes both claims compare-and-set under _state_lock
(`_set_state_if`): the dispatcher claims idle|speaking -> speaking inside its
gate and hands the item back otherwise; start_listening() claims
idle -> listening. Whoever claims first wins.

G1 -- the window held open (deterministic, no interrupt anywhere): the
      dispatcher's locked gate check is answered, and start_listening() is
      called BEFORE it returns, so the dictation begins inside the window.
      Unfixed: the item is spoken over the mic and the state is idle with the
      STT thread alive. Fixed: the dispatcher's claim fails, the item is held
      back, the dictation keeps `listening`, and the item is spoken once the
      user is done.
G2 -- regression guard, the other ordering: a dictation already listening,
      then an item enqueued. Held back, spoken afterwards -- green on both
      sides; it is what G1's fix must not break.
G3 -- regression guard: a queue item playing, then start_listening() -- the
      user is refused with `busy (speaking)` exactly as before (the dispatcher
      claimed first), and nothing about the playing item changes.

exit 0 = all hold; exit 1 = at least one is violated.
"""
import sys
import threading
import time

import harness
import repro_e_interrupt_vs_dictation as e
import repro_f_interrupt_race as f


def build(fake_tts_seconds):
    mod, events, inj = harness.load(fake_tts_seconds=fake_tts_seconds)
    holder = {}
    f.streaming_fakes(mod, holder)
    svc = harness.make_service(mod)
    svc._stt_mode = "streaming"
    inj.available = lambda: False
    holder["proc"] = e.FakeProc(mod.FRAME_BYTES)
    holder["ws"] = e.FakeWS()
    return mod, events, svc, holder


def stt_alive(svc):
    with svc._stt_lock:
        t = svc._stt_thread
    return t is not None and t.is_alive()


def spoken(events, tag):
    return any(ev[0] == "start" and ev[1] == tag for ev in events)


def case_g1():
    mod, events, svc, holder = build(0.05)
    failures = []
    check = e.make_check(failures)
    res = {}
    real_hold = svc._queue_hold_reason

    def hold_in_window():
        r = real_hold()
        # The dispatcher's SECOND call is the one under _queue_current_lock;
        # begin the dictation inside that window, before the gate returns.
        if r is None and svc._queue_current_lock.locked() and "listen" not in res:
            res["listen"] = svc.start_listening(quick=True)
            res["state"] = svc.current_state
        return r

    svc._queue_hold_reason = hold_in_window
    try:
        item = svc.enqueue_speech("AGENT inside the window", source="a")[0]
        harness.wait_for(lambda: "listen" in res, 3.0)
        check(res.get("listen") == "ok",
              f"setup: dictation began inside the gate window ({res.get('listen')!r})")
        check(res.get("state") == "listening",
              f"setup: state was listening as the gate returned ({res.get('state')!r})")
        time.sleep(0.3)   # longer than the fake TTS: an unfixed tree has played it by now
        check(svc.current_state == "listening" and stt_alive(svc),
              f"dictation still listening with its thread alive "
              f"(state {svc.current_state!r}, alive {stt_alive(svc)})")
        check(not spoken(events, "AGENT"),
              f"agent item was NOT spoken over the open mic ({events})")
        check(harness.outcome_of(svc, item) is None,
              f"agent item still pending, not {harness.outcome_of(svc, item)!r}")
        check(all(not t.cancelled for t in svc._cancels.live()),
              "no live token was cancelled")
        svc._queue_hold_reason = real_hold
        check(f.wind_down(svc, holder),
              "wound down to idle with no live token")
        check(spoken(events, "AGENT") and harness.outcome_of(svc, item) == "done",
              f"held item spoken once the user was done ({harness.outcome_of(svc, item)!r})")
    finally:
        svc._queue_hold_reason = real_hold
    return failures


def case_g2():
    mod, events, svc, holder = build(0.05)
    failures = []
    check = e.make_check(failures)
    assert svc.start_listening(quick=True) == "ok", "setup: start_listening refused"
    time.sleep(0.05)
    item = svc.enqueue_speech("AGENT while listening", source="a")[0]
    time.sleep(0.3)
    check(svc.current_state == "listening" and not spoken(events, "AGENT"),
          f"item held behind a live dictation (state {svc.current_state!r}, {events})")
    check(f.wind_down(svc, holder), "wound down to idle with no live token")
    check(harness.outcome_of(svc, item) == "done",
          f"held item spoken afterwards ({harness.outcome_of(svc, item)!r})")
    return failures


def case_g3():
    mod, events, svc, holder = build(0.4)
    failures = []
    check = e.make_check(failures)
    item = svc.enqueue_speech("AGENT playing first", source="a")[0]
    harness.wait_for(lambda: spoken(events, "AGENT"), 3.0)
    r = svc.start_listening(quick=True)
    check(r == "error: busy (speaking)",
          f"a listen during agent playback is refused as before ({r!r})")
    check(not stt_alive(svc), "no STT thread was started")
    harness.wait_for(lambda: harness.outcome_of(svc, item) is not None, 3.0)
    check(harness.outcome_of(svc, item) == "done",
          f"the playing item finished normally ({harness.outcome_of(svc, item)!r})")
    check(harness.wait_for(lambda: svc.current_state == "idle"
                           and not svc._cancels.live(), 3.0),
          "back to idle with no live token")
    return failures


def main():
    print(f"service under test: {harness.SVC_PATH}")
    print("\n  G1: dictation begins inside the dispatcher's gate window (held open)")
    g1 = case_g1()
    print("\n  G2: item enqueued during a live dictation is held (regression guard)")
    g2 = case_g2()
    print("\n  G3: listen during agent playback is refused as before (regression guard)")
    g3 = case_g3()
    print()
    if g1 or g2 or g3:
        why = []
        if g1:
            why.append("dispatcher gate and state claim are not atomic (G1)")
        if g2:
            why.append("item not held behind a live dictation (G2)")
        if g3:
            why.append("listen-during-playback semantics changed (G3)")
        print(f"FAIL: {len(g1) + len(g2) + len(g3)} check(s) -- {' / '.join(why)}")
        return 1
    print("PASS: the dispatcher cannot claim a mic the user is opening.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
