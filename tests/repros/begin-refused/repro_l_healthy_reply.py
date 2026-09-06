#!/usr/bin/env python3
"""Issue #79 regression guard: an UNCANCELLED reply still speaks everything.

The fix reroutes both claim sites through one _claim_playback() and adds an
`aborted` flag plus a precomputed `speak_remainder`.  Every one of those is a
chance to stop speaking a reply nobody cancelled -- the failure mode a user
would notice long before the one the fix is about.

So this asserts the happy path in full: the in-loop sentence AND the
post-stream remainder both reach TTS, subtitles flow, the reply lands in
history, and no token leaks.

Unlike repro_j/repro_k this is NOT a bug repro -- it must pass on origin/main
and on the branch.  A red here on either side is a real regression.

exit 0 = the reply was spoken in full; 1 = something was dropped; 2 = setup.
"""
import sys
import threading
import time

import shim
import harness
import subtitle_spy


def main():
    print(f"service under test: {shim.SVC_PATH}")
    mod, events, _inj = harness.load(fake_tts_seconds=0.4)
    subtitle_spy.assert_isolated(mod)
    spy = subtitle_spy.install_spy(mod)
    svc = harness.make_service(mod)
    states = shim.record_states(svc)

    subtitle_spy.scenario(mod, conversation_mode=True)
    # "Alpha one." completes inside the loop; "Beta two" falls to the
    # remainder block -- both claim sites exercised in one reply.
    mod.stream_chat = lambda **kw: iter(["Alpha one. ", "Beta two"])

    t = threading.Thread(target=svc._conversation_worker,
                         args=("hello",), daemon=True)
    t.start()
    t.join(timeout=20.0)
    if t.is_alive():
        print("  ! the worker never finished")
        return 2
    time.sleep(0.3)

    spoken = [tag for kind, tag, _ts in events if kind == "start"]
    frames = len(spy.frames)
    leaked = svc._cancels.live()
    with svc._conversation_lock:
        history = list(svc._conversation_history)
    replied = [h for h in history if h.get("role") == "assistant"]

    print(f"  sentences sent to TTS:        {spoken}")
    print(f"  subtitle frames emitted:      {frames}")
    print(f"  assistant turns in history:   {len(replied)}")
    print(f"  live tokens left:             {leaked or 'none'}")
    print(f"  final state:                  {svc.current_state!r}")
    print(f"  state transitions:            {states}")

    bad = []
    if "Alpha" not in spoken:
        bad.append("the in-loop sentence was never spoken")
    if "Beta" not in spoken:
        bad.append("the post-stream remainder was never spoken")
    if frames == 0:
        bad.append("no subtitle frames at all")
    if not replied:
        bad.append("the reply never reached conversation history")
    if leaked:
        bad.append(f"leaked tokens: {leaked}")
    if "speaking" not in states:
        bad.append(f"never announced 'speaking' for a real reply "
                   f"(transitions: {states})")
    if svc.current_state != "idle":
        bad.append(f"did not return to idle (state={svc.current_state!r})")
    if bad:
        print("\nFAIL: an uncancelled reply was not delivered in full —")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("\nPASS: the uncancelled reply was spoken in full, subtitled, "
          "recorded, and left no token behind.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
