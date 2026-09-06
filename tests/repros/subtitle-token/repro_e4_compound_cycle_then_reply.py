#!/usr/bin/env python3
"""COMPOSED case: #77's streaming-cycle exits + #42's subtitle token.

Project rule since #77: composed changes need composed tests.  The two changes
do meet, and not where a file-level diff suggests — `_report_recorder_dead`
touches no token and no subtitle, but `_streaming_stt_cycle` **calls**
`_conversation_worker` -> `_stream_conversation_worker` from inside its own
loop (gnome-speaks-service.py ~L2943), before its `finally` retires the cycle's
token.  So at the moment the AI reply issues its own token:

  * the cycle's "stt-stream" token is still LIVE and is the registry's wire
    owner, and
  * the reply's begin() takes the wire from it,

which is precisely the two-live-tokens state that made reading the wire wrong.
The composition this pins is therefore the real one:

  live cycle token -> AI reply speaks -> panic stop -> the NEXT operation
  begins (a loop restart or the resuming speech queue) and lowers the wire

and the assertion is that BOTH HALVES OF THE SERVICE AGREE: the reply's audio
is abandoned AND no subtitle claims the reply finished.  On origin/main the
subtitle half disagrees.

The cycle's token is issued directly rather than by driving the whole recorder
+ WebSocket cycle: the composition under test is the registry state at the call
site, and faking the recorder would import another suite's fixtures that are
themselves being rewritten this hour.  The call into `_conversation_worker` is
the real one.

exit 0 = both halves agree; 1 = they disagree; 2 = setup failure.
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

    subtitle_spy.scenario(mod, conversation_mode=True)
    mod.stream_chat = lambda **kw: iter(["Alpha one. ", "Beta two. "])

    # The state _streaming_stt_cycle is in when it calls _conversation_worker:
    # its own token live, registered, owning the wire, not yet retired.
    cycle_token = svc._cancels.issue("stt-stream")
    assert svc._cancels.begin(cycle_token), "the cycle should own the wire"

    threading.Thread(target=svc._conversation_worker,
                     args=("hello",), daemon=True).start()

    if not harness.wait_for(lambda: len(spy.frames_for("Alpha")) >= 2, 10.0):
        print("  ! the AI reply never produced subtitle frames")
        return 2
    reply_token = svc._speak_token
    if reply_token is None or reply_token is cycle_token:
        print("  ! SETUP FAILURE — the reply did not issue its own token")
        return 2
    live = {t.label for t in svc._cancels.live()}
    print(f"  two live tokens at the composition point: {sorted(live)}")

    spy.arm_stall(STALL)
    if not spy.stalling.wait(5.0):
        print("  ! the stall never armed")
        return 2

    # Panic stop: cancel_all() records a verdict on BOTH tokens and raises the
    # wire; then the next operation (loop restart / resuming queue) begins.
    threading.Thread(target=svc.stop, daemon=True).start()
    if not harness.wait_for(lambda: any(k == "cancel" for k, _t, _ts in events), 3.0):
        print("  ! SETUP FAILURE — the reply's tts never observed the stop")
        return 2
    # Measured, not asserted: the fake tts logs a "cancel" event only when it
    # actually observed the stop and returned {"cancelled": True}.
    audio_abandoned = any(k == "cancel" for k, _t, _ts in events)
    nxt = svc._cancels.issue("speech")
    svc._cancels.begin(nxt)

    if not spy.stall_done.wait(5.0):
        print("  ! the stalled frame never returned")
        return 2

    harness.wait_for(lambda: spy.completed("Alpha"), 2.5)
    subtitle_claims_finished = spy.completed("Alpha")

    svc._cancels.retire(nxt)
    svc._cancels.retire(cycle_token)
    mod.state.cancel_active()
    time.sleep(0.3)

    print(f"  reply token cancelled:        {reply_token.cancelled}")
    print(f"  cycle token cancelled:        {cycle_token.cancelled}")
    print(f"  wire at the subtitle's final check: "
          f"{'UP' if spy.wire_at_stall_exit else 'DOWN (the next operation took it)'}")
    print(f"  audio half  — reply abandoned mid-sentence: {audio_abandoned}")
    print(f"  subtitle half — reply reported finished:    {subtitle_claims_finished}")
    print(f"  'Alpha one.' frames: {[p for _t, p, _ts in spy.frames_for('Alpha')]}")

    if not reply_token.cancelled or not cycle_token.cancelled:
        print("  ! SETUP FAILURE — stop() did not cancel both live tokens")
        return 2
    if spy.wire_at_stall_exit:
        print("  ! SETUP FAILURE — the wire was never lowered, so the "
              "composed interleaving did not happen")
        return 2

    if subtitle_claims_finished:
        print("\nFAIL: the two halves disagree — the reply's audio was "
              "abandoned mid-sentence while its subtitle reported 100% "
              "finished, because the subtitle read a wire that by then "
              "belonged to the next operation.")
        return 1
    print("\nPASS: audio abandoned and no subtitle claimed completion — both "
          "halves of the service agree across the composed path.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
