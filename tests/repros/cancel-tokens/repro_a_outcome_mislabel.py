#!/usr/bin/env python3
"""Issue #21 consequence (a): a preempted queue item is recorded 'done'.

THE INTERLEAVING (all real code; only the width of one window is nudged)

  1. The dispatcher is playing queue item AAA.  speech_tts.tts polls the
     process-global wire, state._cancel_event.
  2. Something cancels: /skip, POST /stop, or -- the case here -- the user
     speaking, which runs stop(drain_queue=False) -> state.cancel_active().
  3. AAA's tts returns {"cancelled": True}.  _speak_worker's finally then runs
     (HTTP progress reset, state read, _schedule_warmup) BEFORE the dispatcher
     gets to classify the outcome.
  4. In that window the preempting user worker enters _speak_worker and runs
     `state._cancel_event.clear()`.  The wire is now down.
  5. The dispatcher finally reaches `if state._cancel_event.is_set()` and
     reads False -> records AAA as "done".

  GET /queue therefore tells an agent its utterance was spoken in full when it
  was cut off mid-word.  The window is real (a lock, a state read, a warmup
  schedule); this script widens it deterministically by making the already
  stubbed _schedule_warmup take 250 ms, and starts the preempting user speech
  the instant AAA's tts observes the cancel.

exit 0 = every preempted item labelled "interrupted"; exit 1 = mislabelled.
"""
import sys
import threading
import time

import harness

ATTEMPTS = 3
WARMUP_STALL = 0.25


def attempt(n):
    mod, events, _inj = harness.load(fake_tts_seconds=3.0)
    svc = harness.make_service(mod)

    # Model the real cost of _speak_worker's finally.  The dispatcher cannot
    # classify the outcome until this returns.
    mod._schedule_warmup = lambda *a, **k: time.sleep(WARMUP_STALL)

    item_id, _pos, _dropped = svc.enqueue_speech(f"AAA{n} queue item")

    if not harness.wait_for(lambda: any(k == "start" for k, _t, _ts in events), 5.0):
        print("  ! dispatcher never started the item")
        return None, False

    time.sleep(0.3)

    preempt_started = threading.Event()

    def preempt():
        # The real user path: hold, stop(drain_queue=False), new _speak_worker
        # whose entry clears the wire.
        preempt_started.set()
        svc.speak(f"BBB{n} user speech")

    # Fire the user preempt the moment AAA's tts observes the cancel, so its
    # entry-clear lands inside AAA's finally.
    def on_cancel():
        harness.wait_for(
            lambda: any(k == "cancel" for k, _t, _ts in events), 5.0)
        preempt()

    t = threading.Thread(target=on_cancel, daemon=True)
    t.start()
    svc.stop(drain_queue=False)          # what a /skip or user preempt does
    preempt_started.wait(5.0)

    harness.wait_for(lambda: harness.outcome_of(svc, item_id) is not None, 5.0)
    outcome = harness.outcome_of(svc, item_id)

    saw_cancel = any(k == "cancel" for k, _t, _ts in events)
    mod.state.cancel_active()            # let BBB end early; keep the run short
    time.sleep(0.2)
    return outcome, saw_cancel


def main():
    print(f"service under test: {harness.SVC_PATH}")
    bad = 0
    for n in range(1, ATTEMPTS + 1):
        outcome, saw_cancel = attempt(n)
        if not saw_cancel:
            print(f"  attempt {n}: SETUP FAILURE — tts never observed the cancel")
            return 2
        ok = outcome == "interrupted"
        if not ok:
            bad += 1
        print(f"  attempt {n}: cancelled mid-play -> outcome={outcome!r} "
              f"{'OK' if ok else 'MISLABELLED (expected interrupted)'}")
    print(f"\nmislabelled: {bad}/{ATTEMPTS}")
    if bad:
        print("FAIL: a cancelled utterance is reported to /queue as 'done'.")
        return 1
    print("PASS: every preempted item is labelled 'interrupted'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
