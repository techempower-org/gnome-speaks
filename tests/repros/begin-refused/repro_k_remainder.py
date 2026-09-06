#!/usr/bin/env python3
"""Issue #79, second claim site: the REMAINDER-only reply.

A reply that never produces a sentence boundary (no . ! ?) skips the in-loop
claim entirely and is spoken by the post-stream remainder block, which had its
own copy of the ignored begin().  Fixing only the first site would leave the
short-answer case -- "Yes", "42", "Done" -- still speaking after a stop, and
short answers are exactly what a voice assistant emits most.

Same contract as repro_j: after a stop in the claim window, NO audio and NO
subtitle frames.

exit 0 = nothing spoken, nothing subtitled; 1 = the stop was ignored; 2 = setup.
"""
import sys
import threading
import time

import shim
import harness
import subtitle_spy


def main():
    print(f"service under test: {shim.SVC_PATH}")
    mod, events, _inj = harness.load(fake_tts_seconds=3.0)
    subtitle_spy.assert_isolated(mod)
    spy = subtitle_spy.install_spy(mod)
    svc = harness.make_service(mod)
    states = shim.record_states(svc)

    subtitle_spy.scenario(mod, conversation_mode=True)
    # One token, no sentence terminator followed by whitespace, so
    # _split_sentences yields nothing and the whole reply falls to the
    # post-stream remainder block -- the SECOND claim site.
    mod.stream_chat = lambda **kw: iter(["Alpha one"])

    hook = shim.stop_in_the_claim_window(svc)

    t = threading.Thread(target=svc._conversation_worker,
                         args=("hello",), daemon=True)
    t.start()
    t.join(timeout=20.0)
    if t.is_alive():
        print("  ! the worker never finished")
        return 2

    time.sleep(0.3)   # let any late subtitle frame land before asserting none

    if not hook["fired"]:
        print("  ! SETUP FAILURE — the claim window was never reached")
        return 2
    token = hook["token"]
    if not token.cancelled:
        print("  ! SETUP FAILURE — the token was not cancelled in the window")
        return 2

    spoken = [tag for kind, tag, _ts in events if kind == "start"]
    frames = spy.frames
    leaked = svc._cancels.live()

    print(f"  stop landed inside the claim window: {hook['fired']}")
    print(f"  begin() would refuse (token cancelled): {token.cancelled}")
    print(f"  audio half    — sentences sent to TTS: {spoken or 'none'}")
    print(f"  subtitle half — frames emitted:        "
          f"{[(t_[:12], p) for t_, p, _ in frames] or 'none'}")
    print(f"  live tokens left in the registry:      {leaked or 'none'}")
    print(f"  badge half    — state transitions:     {states}")

    bad = []
    if spoken:
        bad.append(f"{len(spoken)} sentence(s) spoken after the stop: {spoken}")
    if frames:
        bad.append(f"{len(frames)} subtitle frame(s) emitted after the stop")
    if leaked:
        bad.append(f"leaked tokens: {leaked}")
    if "speaking" in states:
        bad.append("the badge announced 'speaking' for a reply that was "
                   f"never spoken (transitions: {states})")
    if bad:
        print("\nFAIL: begin() refused and the reply played anyway —")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("\nPASS: the stop in the claim window abandoned the reply — no audio, "
          "no subtitle frames, no leaked token.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
