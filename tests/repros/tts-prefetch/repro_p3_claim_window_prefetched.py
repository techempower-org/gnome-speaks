#!/usr/bin/env python3
"""COMPOSED: #134's prefetch x #79's begin() refusal (begin-refused j/k).

With prefetch, the first sentence is SYNTHESIZED before the reply claims
playback -- the claim (issue -> _speak_token -> begin) now happens on the
speaker thread with a prepared handle in hand.  If a stop lands in that claim
window, begin() refuses, and the service must:

  * close the prepared handle, never play it      (no "play-start", "closed")
  * emit no subtitle frame
  * never announce "speaking"
  * leave no live token

Same hook as j/k (shim.stop_in_the_claim_window: the cancel is planted inside
issue() so the window is hit every time instead of raced for).  The new half
is the setup check: the sentence's synthesis COMPLETED before the claim fired
-- that is the prepared-ahead handle #79's repros could not have, because on
their service nothing was synthesized before begin() said yes.

On the pre-#134 baseline begin() refuses before tts() is ever called, so there
is no prepared handle and this reports 2 (window absent), not 0.

exit 0 = refused cleanly with the handle closed; 1 = the prepared sentence
played, subtitled or announced; 2 = setup.
"""
import sys
import time

import prefetch_harness as ph

S, P = 0.3, 1.0
TOKENS = ["Alpha one is here.", " Beta two follows."]


def main():
    print(f"service under test: {ph.SVC_PATH}")
    mod, tl, _inj = ph.load(synth_seconds=S, play_seconds=P)
    ph.subtitle_spy.assert_isolated(mod)
    spy = ph.subtitle_spy.install_spy(mod)
    svc = ph.make_service(mod)
    states = ph.record_states(svc)
    ph.subtitle_spy.scenario(mod, conversation_mode=True)

    hook = ph.stop_in_the_claim_window(svc)
    t_claim = {"t": None}
    real_issue = svc._cancels.issue

    def timed_issue(lbl):
        if lbl == "ai-reply" and t_claim["t"] is None:
            t_claim["t"] = tl.now()
        return real_issue(lbl)

    svc._cancels.issue = timed_issue

    t = ph.run_reply(mod, svc, TOKENS)
    t.join(timeout=20.0)
    if t.is_alive():
        print("  ! the worker never finished")
        return 2
    time.sleep(0.3)

    if not hook["fired"] or t_claim["t"] is None:
        print("  ! SETUP FAILURE — the claim window was never reached")
        return 2
    if not hook["token"].cancelled:
        print("  ! SETUP FAILURE — the token was not cancelled in the window")
        return 2
    se_a = tl.first("synth-end", "Alpha")
    if se_a is None or not se_a <= t_claim["t"]:
        print("  ! SETUP FAILURE — Alpha was not synthesized before the claim; "
              "no prepared-ahead handle exists on this service")
        print(f"  timeline: {tl.dump()}")
        return 2

    played = tl.of_kind("play-start")
    closed = tl.first("closed", "Alpha") is not None
    frames = spy.frames
    leaked = svc._cancels.live()

    print(f"  Alpha synthesized at t={se_a:.3f}, claim at t={t_claim['t']:.3f}, "
          f"stop planted inside it: {hook['fired']}")
    print(f"  audio half    — playback started: {played or 'none'}; "
          f"prepared handle closed: {closed}")
    print(f"  subtitle half — frames emitted:   "
          f"{[(x[:12], p) for x, p, _ in frames] or 'none'}")
    print(f"  badge half    — state transitions: {states}")
    print(f"  live tokens left: {leaked or 'none'}")
    print(f"  timeline: {tl.dump()}")

    bad = []
    if played:
        bad.append(f"a prepared sentence was played after begin() refused: {played}")
    if not closed:
        bad.append("the prepared sentence was never closed")
    if frames:
        bad.append(f"{len(frames)} subtitle frame(s) for a refused reply")
    if "speaking" in states:
        bad.append(f"'speaking' announced for a refused reply: {states}")
    if leaked:
        bad.append(f"leaked tokens: {leaked}")
    if bad:
        print("\nFAIL: begin() refused with a prepared sentence in hand and —")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("\nPASS: begin() refused — the prepared sentence was closed unplayed, "
          "no frame, no 'speaking', no token left.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
