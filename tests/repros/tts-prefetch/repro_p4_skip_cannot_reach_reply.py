#!/usr/bin/env python3
"""Guard (#134 x #146): a queue skip / interrupt cannot reach a prefetched reply.

#146 made `interrupt:true` on POST /speak call skip_current(), which cancels
the QUEUE's current token (`_queue_token`) under `_queue_current_lock`. The
question raised in review: could that cancel land on an AI reply whose next
sentence is already synthesized, and leave the prefetch in an odd state?

Measured here rather than argued: an AI reply holds the speech queue for the
whole turn (`_hold_user_speech`), so while it speaks the dispatcher is parked,
`_queue_current` is None, and skip_current() returns None without touching
any token. The reply's own token (`ai-reply`, published as `_speak_token`) is
never the queue token. The prefetched sentence therefore plays normally after
the skip, and every sentence completes.

Also asserts the negative space the review asked about: the QUEUE path
(`_tts_dispatcher` -> `_speak_worker`) has NO prefetch -- it still calls
speech_tts.tts() per item -- so there is no prefetched N+1 for a skipped queue
item to leave playing.

exit 0 = the skip is a no-op for the reply and the reply completes; 1 = a skip
reached the reply (a sentence was cancelled or dropped); 2 = setup.
"""
import inspect
import sys
import time

import prefetch_harness as ph

S, P = 0.3, 0.8
TOKENS = ["Alpha one is here.", " Beta two follows.", " Gamma three ends."]


def main():
    print(f"service under test: {ph.SVC_PATH}")
    mod, tl, _inj = ph.load(synth_seconds=S, play_seconds=P)
    ph.subtitle_spy.assert_isolated(mod)
    svc = ph.make_service(mod)
    ph.subtitle_spy.scenario(mod, conversation_mode=True)

    t = ph.run_reply(mod, svc, TOKENS)
    if not ph.wait_for(lambda: tl.first("play-start", "Alpha") is not None, 10.0):
        print("  ! SETUP FAILURE — Alpha never started playing")
        return 2
    if not ph.wait_for(lambda: tl.first("synth-end", "Beta") is not None, P - 0.1):
        print("  ! SETUP FAILURE — Beta was not prepared while Alpha played")
        return 2
    reply_token = svc._speak_token
    with svc._queue_current_lock:
        queue_current = svc._queue_current
        queue_token = svc._queue_token

    # The review's scenario: a skip lands while the reply speaks and its next
    # sentence is prepared. Unscoped, like the interrupt branch calls it.
    skipped = svc.skip_current()
    t_skip = tl.now()

    t.join(timeout=20.0)
    if t.is_alive():
        print("  ! the worker never finished")
        return 2
    time.sleep(0.2)

    starts = [g for g, _ in tl.of_kind("play-start")]
    ends = [g for g, _ in tl.of_kind("play-end")]
    cancels = tl.of_kind("cancel")
    closed = tl.of_kind("closed")
    leaked = svc._cancels.live()

    # Negative space: the queue path has no prefetch to leave behind.
    src = inspect.getsource(svc._speak_worker)
    queue_uses_seam = "tts_prepare" in src or "_PreparedSentence" in src

    print(f"  during the reply: _queue_current={queue_current} _queue_token={queue_token}")
    print(f"  skip_current() returned: {skipped!r} at t={t_skip:.3f}")
    print(f"  reply token: {reply_token!r} cancelled={reply_token.cancelled}")
    print(f"  played: {starts}; finished: {ends}; cancels: {cancels or 'none'}; closed: {closed or 'none'}")
    print(f"  leaked tokens: {leaked or 'none'}")
    print(f"  queue path (_speak_worker) uses the prefetch seam: {queue_uses_seam}")
    print(f"  timeline: {tl.dump()}")

    if queue_current is not None or queue_token is not None:
        print("  ! SETUP FAILURE — the queue was not held during the reply")
        return 2

    bad = []
    if skipped is not None:
        bad.append(f"skip_current() found something to skip during a reply: {skipped!r}")
    if reply_token.cancelled:
        bad.append("the skip cancelled the reply's token")
    if starts != ["Alpha", "Beta", "Gamma"] or ends != ["Alpha", "Beta", "Gamma"]:
        bad.append(f"the reply did not complete in order after the skip: {starts} / {ends}")
    if cancels or closed:
        bad.append(f"a sentence was cancelled or dropped: cancels={cancels} closed={closed}")
    if leaked:
        bad.append(f"leaked tokens: {leaked}")
    if queue_uses_seam:
        bad.append("the queue path now prefetches — this guard's premise no longer holds; "
                   "write the skipped-item-drops-its-prefetch case before relying on it")
    if bad:
        print("\nFAIL: a queue skip reached the AI reply —")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("\nPASS: skip_current() is a no-op while a reply holds the queue; the "
          "prefetched sentence played in order and the queue path has no prefetch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
