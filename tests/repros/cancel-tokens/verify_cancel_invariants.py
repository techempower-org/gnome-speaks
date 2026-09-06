#!/usr/bin/env python3
"""Invariants the cancel-token layer must hold (issue #21).

  I1  Every issued token is retired.  A leaked token is not cosmetic: it stays
      in the live set forever, so cancel_all() keeps "cancelling" a dead
      operation and begin() logs a stale warning on every later utterance.
  I2  When nothing is running, the global wire is DOWN.  A stale cancel left
      raised is what makes the next operation die on arrival.
  I3  Exactly one terminal outcome per queue item id (the #19 contract), with
      cancel/skip/stop mixed in.
  I4  A skip lands on the item it named, and that item alone.

Exercises the queue, /skip, /stop, user preemption and batch STT together --
the paths that share the one wire.

Reports SKIP on a build without the registry (pre-#21), so the suite can be
pointed at origin/main without a false failure.
"""
import sys
import time

import harness


def main():
    print(f"service under test: {harness.SVC_PATH}")
    mod, _events, inj = harness.load(fake_tts_seconds=0.4)
    svc = harness.make_service(mod)

    if not hasattr(svc, "_cancels"):
        print("SKIP: this build has no cancel registry (pre-#21).")
        return 0

    failures = []

    def check(ok, label):
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        if not ok:
            failures.append(label)

    # --- a burst of queue items, one of them skipped by id -----------------
    ids = [svc.enqueue_speech(f"Q{n} queued item", source="agent")[0]
           for n in range(6)]
    harness.wait_for(lambda: svc._queue_current is not None, 5.0)
    playing = svc._queue_current.id
    skipped = svc.skip_current(playing)
    check(skipped == playing, f"skip_current({playing}) hit the playing item")

    # I4 is checked here, in isolation: the panic stop below legitimately
    # interrupts whatever is playing by then, which would mask a cascade.
    harness.wait_for(lambda: harness.outcome_of(svc, playing) is not None, 5.0)
    after_skip = {r["id"]: r["outcome"] for r in list(svc._queue_recent)}
    check(after_skip.get(playing) == "interrupted",
          f"I4 the skipped item is 'interrupted' (got {after_skip.get(playing)!r})")
    check(set(after_skip) == {playing},
          f"I4 the skip touched that item and no other ({after_skip})")
    check(svc.skip_current(999999) is None, "skip_current(wrong id) is a no-op")

    # --- user speech preempts the rest, then a panic stop ------------------
    time.sleep(0.3)
    svc.speak("USER preempting speech")
    time.sleep(0.3)
    svc.stop()

    # --- a batch STT session that is stopped while "uploading" -------------
    svc._stt_mode = "fixed"

    def slow_stt(mode=None, **kw):
        time.sleep(1.0)
        return {"text": "late transcript"}

    mod.stt_dispatch = slow_stt
    svc.start_listening()
    time.sleep(0.2)
    svc.stop()

    # --- let everything wind down ------------------------------------------
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not svc._cancels.live() and svc._queue_current is None:
            break
        time.sleep(0.05)
    time.sleep(0.5)

    live = svc._cancels.live()
    check(not live, f"I1 no leaked tokens (live={[repr(t) for t in live]})")
    check(not mod.state._cancel_event.is_set(),
          "I2 the global wire is down once nothing is running")

    outcomes = [(r["id"], r["outcome"]) for r in list(svc._queue_recent)]
    seen = {}
    dupes = []
    for item_id, outcome in outcomes:
        if item_id in seen:
            dupes.append(item_id)
        seen[item_id] = outcome
    check(not dupes, f"I3 one terminal outcome per id (dupes={dupes})")
    check(all(i in seen for i in ids),
          "I3 every enqueued id reached a terminal outcome")
    check(all(o in ("canceled", "done", "interrupted") for o in seen.values()),
          f"I3 every outcome is from the terminal vocabulary ({seen})")
    check(not [t for t, _ts in inj.all_text()],
          "no STT transcript reached the cursor across the whole run")

    print(f"\noutcomes: {outcomes}")
    if failures:
        print(f"FAIL: {len(failures)} invariant(s) violated.")
        return 1
    print("RESULT: CANCEL INVARIANTS HOLD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
