#!/usr/bin/env python3
"""Issue #42 (a): a cancelled utterance still emits its 100 % "finished" frame.

THE INTERLEAVING -- all real code paths; only one real window is widened.

  1. The queue dispatcher is playing agent item AAA.  _speak_worker started a
     subtitle thread that emits a SubtitleUpdate every 200 ms and, when the
     loop ends, one final frame at 100 %.
  2. The user speaks.  The real preempt is stop(drain_queue=False) followed by
     the user's own utterance: cancel_all() records the verdict "cancelled" on
     AAA's token and raises the process-global wire (state._cancel_event).
     AAA's tts returns {"cancelled": True} and _speak_worker's finally sets
     sub_stop.
  3. The user's utterance BBB enters _speak_worker and calls
     CancelRegistry.begin(), which LOWERS the wire -- correctly, because BBB is
     not cancelled.  The wire is one process-global bit; it now says nothing
     about AAA.
  4. AAA's subtitle thread reaches its final check.  On origin/main that check
     is `if not state._cancel_event.is_set()`, which now reads False, so it
     emits 100 % for an utterance that was cut off mid-word and is recorded in
     GET /queue as "interrupted".

  Step 4 is a narrow window on its own.  This script pins it open by stalling
  ONE frame's GLib.idle_add -- which is not a fiction: publishing to a busy
  GNOME Shell main loop really does block the emitting thread.  Everything
  else is the real code: svc.stop(drain_queue=False) and svc.speak() are the
  user-preempt path, and BBB is only started once AAA's tts has actually
  observed the cancel (the same rendezvous repro_a uses), so the sequence
  cancel -> observe -> begin() is fixed rather than lucky.

  The run is only meaningful if the wire really was down when the subtitle
  thread regained control -- otherwise origin/main would pass for the wrong
  reason -- so that is checked and reported as a SETUP FAILURE, not a pass.

exit 0 = no stale 100 % frame; 1 = bug present; 2 = setup failure.
"""
import sys
import threading
import time

import subtitle_spy
import harness

STALL = 0.9


def main():
    print(f"service under test: {subtitle_spy.SVC_PATH}")

    mod, events, _inj = harness.load(fake_tts_seconds=4.0)
    subtitle_spy.assert_isolated(mod)
    spy = subtitle_spy.install_spy(mod)
    svc = harness.make_service(mod)
    print(f"  token-aware subtitle thread: {subtitle_spy.subtitle_takes_token(mod)}")

    item_id, _pos, _dropped = svc.enqueue_speech("AAA agent queue utterance")
    if not harness.wait_for(lambda: any(k == "start" for k, _t, _ts in events), 5.0):
        print("  ! dispatcher never started the item")
        return 2
    if not harness.wait_for(lambda: len(spy.frames_for("AAA")) >= 2, 5.0):
        print("  ! subtitle progress never emitted -- cannot observe anything")
        return 2
    print(f"  AAA playing, {len(spy.frames_for('AAA'))} progress frames so far")

    # Pin open the window between sub_stop.set() and the final check.
    spy.arm_stall(STALL)
    if not spy.stalling.wait(5.0):
        print("  ! the stall never armed")
        return 2

    # The user preempt, in its real order, with BBB held until AAA's tts has
    # actually seen the cancel so that begin() cannot precede the observation.
    def preempt():
        if harness.wait_for(lambda: any(k == "cancel" for k, _t, _ts in events), 3.0):
            svc.speak("BBB user speech")

    threading.Thread(target=preempt, daemon=True).start()
    svc.stop(drain_queue=False)

    if not spy.stall_done.wait(5.0):
        print("  ! the stalled frame never returned")
        return 2

    harness.wait_for(lambda: harness.outcome_of(svc, item_id) is not None, 5.0)
    outcome = harness.outcome_of(svc, item_id)
    # Give any final frame time to land before declaring there is none.
    harness.wait_for(lambda: spy.completed("AAA"), 1.5)
    stale = spy.completed("AAA")

    saw_cancel = any(k == "cancel" for k, _t, _ts in events)
    mod.state.cancel_active()          # let BBB end early; keep the run short
    time.sleep(0.2)

    print(f"  AAA's tts observed the cancel: {saw_cancel}")
    print(f"  wire when AAA's subtitle thread regained control: "
          f"{'UP' if spy.wire_at_stall_exit else 'DOWN (BBB begin() lowered it)'}")
    print(f"  AAA outcome recorded for GET /queue: {outcome!r}")
    print(f"  AAA subtitle frames: {[p for _t, p, _ts in spy.frames_for('AAA')]}")

    if not saw_cancel or outcome != "interrupted":
        print("  ! SETUP FAILURE — AAA was not actually cancelled mid-play")
        return 2
    if spy.wire_at_stall_exit:
        print("  ! SETUP FAILURE — the preempt's begin() never lowered the wire, "
              "so the interleaving under test did not happen")
        return 2

    if stale:
        print("\nFAIL: a cancelled utterance emitted its 100% 'finished' "
              "subtitle frame — the final check read the wire, which by then "
              "belonged to the preempting utterance.")
        return 1
    print("\nPASS: the cancelled utterance emitted no 100% frame; the subtitle "
          "thread judged by its own token.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
