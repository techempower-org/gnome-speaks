#!/usr/bin/env python3
"""Issue #42, second call path: the streaming AI reply's subtitle queue.

_stream_conversation_worker does not start a subtitle thread per sentence; it
puts items on subtitle_q for one long-lived _subtitle_queue_worker.  The token
therefore has to travel through the queue tuple, which is both the fix and the
regression risk: an arity mismatch between put() and the unpack would kill that
daemon thread on the first sentence and silently take AI-reply subtitles with
it.  This script drives the real worker with a stubbed stream_chat so both are
covered:

  * PLUMBING -- sentences really do produce subtitle frames through the queue.
  * VERDICT  -- the reply's token is cancelled and then an unrelated operation
    calls begin(), lowering the shared wire.  On origin/main the in-flight
    sentence's final check reads that lowered wire and emits 100 % for a reply
    the user cancelled; the token says otherwise.

The one widened window is the same stalled GLib.idle_add as repro_e1.

exit 0 = clean; 1 = bug present; 2 = setup failure.
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

    # Two tokens: the second completes the first sentence, so "Alpha one." is
    # spoken while the stream is still open — the in-flight case we need.
    mod.stream_chat = lambda **kw: iter(["Alpha one. ", "Beta two. "])

    worker = threading.Thread(target=svc._stream_conversation_worker,
                              args=("hello",), daemon=True)
    worker.start()

    if not harness.wait_for(lambda: len(spy.frames_for("Alpha")) >= 2, 8.0):
        print("  ! no subtitle frames from the streaming reply — the queue "
              "worker never ran (tuple arity?)")
        return 1
    print(f"  streaming subtitles flowing: {len(spy.frames_for('Alpha'))} frames")

    reply_token = svc._speak_token
    if reply_token is None:
        print("  ! SETUP FAILURE — no ai-reply token was issued")
        return 2

    spy.arm_stall(STALL)
    if not spy.stalling.wait(5.0):
        print("  ! the stall never armed")
        return 2

    # The user cancels the reply, and only once the in-flight tts has actually
    # observed it does an unrelated operation take the wire — so the sequence
    # cancel -> observe -> begin() is fixed rather than lucky.
    svc._cancels.cancel(reply_token, kill_procs=False)
    if not harness.wait_for(lambda: any(k == "cancel" for k, _t, _ts in events), 3.0):
        print("  ! SETUP FAILURE — the sentence's tts never observed the cancel")
        return 2
    other = svc._cancels.issue("speech")
    svc._cancels.begin(other)

    if not spy.stall_done.wait(5.0):
        print("  ! the stalled frame never returned")
        return 2

    harness.wait_for(lambda: spy.completed("Alpha"), 2.5)
    stale = spy.completed("Alpha")

    svc._cancels.retire(other)
    mod.state.cancel_active()          # end the remainder early
    worker.join(timeout=5.0)
    time.sleep(0.2)

    print(f"  reply token cancelled: {reply_token.cancelled}")
    print(f"  wire when the subtitle thread regained control: "
          f"{'UP' if spy.wire_at_stall_exit else 'DOWN (an unrelated begin() lowered it)'}")
    print(f"  'Alpha one.' subtitle frames: "
          f"{[p for _t, p, _ts in spy.frames_for('Alpha')]}")

    if not reply_token.cancelled:
        print("  ! SETUP FAILURE — the reply was never cancelled")
        return 2
    if spy.wire_at_stall_exit:
        print("  ! SETUP FAILURE — the wire was never lowered, so the "
              "interleaving under test did not happen")
        return 2

    if stale:
        print("\nFAIL: a cancelled AI reply emitted its 100% 'finished' "
              "subtitle frame — the queued sentence read the shared wire.")
        return 1
    print("\nPASS: streaming subtitles flow through the queue and stop for "
          "their own reply's verdict.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
