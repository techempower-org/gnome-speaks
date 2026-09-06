#!/usr/bin/env python3
"""Issue #42 (b): another operation's cancel freezes THIS subtitle mid-word.

A contract test of the seam the issue names, not a claim about one production
caller: _run_subtitle_progress belongs to exactly one utterance and must stop
for that utterance's cancellation only.

  1. Utterance AAA issues a token and begins; its subtitle thread runs.
  2. A DIFFERENT live operation is cancelled -- registry.cancel(other), which
     records that operation's verdict and raises the shared wire.  AAA's token
     is untouched: AAA was not cancelled.
  3. On origin/main AAA's subtitle thread reads `state._cancel_event.is_set()`
     at the top of its loop, sees a bit that is not about it, and breaks -- the
     subtitle freezes mid-word and the final 100 % frame is suppressed even
     though AAA runs to completion.

kill_procs=False is the registry's own code path (CancelRegistry._raise_wire);
it keeps the harness from signalling subprocesses.  The wire semantics -- the
only thing under test here -- are identical either way.

exit 0 = the subtitle ignored the foreign cancel; 1 = bug present.
"""
import sys
import threading
import time

import subtitle_spy
import harness

EST_DURATION = 6.0


def main():
    print(f"service under test: {subtitle_spy.SVC_PATH}")
    mod, _events, _inj = harness.load(fake_tts_seconds=1.0)
    subtitle_spy.assert_isolated(mod)
    spy = subtitle_spy.install_spy(mod)
    svc = harness.make_service(mod)
    print(f"  token-aware subtitle thread: {subtitle_spy.subtitle_takes_token(mod)}")

    token = svc._cancels.issue("speech")
    assert svc._cancels.begin(token), "AAA should own the wire"

    stop_ev = threading.Event()
    t = threading.Thread(
        target=subtitle_spy.run_subtitle_progress,
        args=(mod, svc, "AAA live utterance", EST_DURATION, stop_ev, token),
        daemon=True)
    t.start()

    if not harness.wait_for(lambda: len(spy.frames_for("AAA")) >= 2, 5.0):
        print("  ! subtitle progress never emitted -- cannot observe anything")
        return 2
    before = len(spy.frames_for("AAA"))

    # A different operation is cancelled. AAA's token is NOT touched.
    other = svc._cancels.issue("stt")
    svc._cancels.cancel(other, kill_procs=False)
    assert not token.cancelled, "AAA's own verdict must be untouched"
    print(f"  foreign cancel raised the wire; AAA cancelled={token.cancelled}")

    time.sleep(0.7)                    # >= 3 more 200 ms ticks
    after = len(spy.frames_for("AAA"))

    stop_ev.set()                      # AAA's playback finished normally
    t.join(timeout=2.0)
    completed = spy.completed("AAA")

    svc._cancels.retire(other)
    svc._cancels.retire(token)         # drops the wire (owner)

    kept_going = after - before
    print(f"  progress frames after the foreign cancel: {kept_going}")
    print(f"  final 100% frame emitted: {completed}")

    if kept_going == 0 or not completed:
        print("\nFAIL: an unrelated operation's cancel froze this utterance's "
              "subtitle" + ("" if kept_going else " (0 further frames)") +
              ("" if completed else " and suppressed its 100% frame") + ".")
        return 1
    print("\nPASS: the subtitle ignored the foreign cancel and finished at 100%.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
