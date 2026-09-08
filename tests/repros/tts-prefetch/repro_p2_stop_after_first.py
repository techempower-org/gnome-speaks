#!/usr/bin/env python3
"""Issue #134, the cancel case: a stop after sentence 1 must drop the prefetch.

Prefetching means that when the user says stop during sentence 1, sentence 2
is ALREADY SYNTHESIZED and waiting.  The issue's hedge, carried verbatim:
"it must still be *unplayed*, which the token check guarantees."  This repro
is that guarantee, measured:

  * sentence 2 was prepared BEFORE the stop        (setup: the window exists)
  * after the stop: NO synthesis starts            (the producer stops preparing)
  * after the stop: NO playback starts             (the speaker closes, never plays)
  * the prepared sentence 2 was closed, not played
  * sentence 1's playback observed the stop        (fake logs "cancel")
  * the reply's token is cancelled, none leaked
  * subtitles: no 100% frame for sentence 1, no frame at all for sentence 2

The stop is svc.stop() from another thread -- the real panic stop, which sets
_stop_event, writes every live verdict and raises the wire -- landing while
sentence 1 is mid-playback (same rendezvous discipline as subtitle-token e4).

On the pre-#134 baseline sentence 2 is never prepared before sentence 1 ends,
so the window under test does not exist there and this reports 2, not 0.

exit 0 = prefetch dropped cleanly; 1 = audio or synthesis after the stop;
2 = setup.
"""
import sys
import threading
import time

import prefetch_harness as ph

S, P = 0.3, 1.5
TOKENS = ["Alpha one is here.", " Beta two follows.", " Gamma three goes on.",
          " Delta four ends."]


def main():
    print(f"service under test: {ph.SVC_PATH}")
    mod, tl, _inj = ph.load(synth_seconds=S, play_seconds=P)
    ph.subtitle_spy.assert_isolated(mod)
    spy = ph.subtitle_spy.install_spy(mod)
    svc = ph.make_service(mod)
    ph.subtitle_spy.scenario(mod, conversation_mode=True)

    t = ph.run_reply(mod, svc, TOKENS)

    # Rendezvous: Alpha is playing AND Beta has been prepared under it. Only
    # then is there a prefetched sentence for the stop to drop.
    if not ph.wait_for(lambda: tl.first("play-start", "Alpha") is not None, 10.0):
        print("  ! SETUP FAILURE — Alpha never started playing")
        return 2
    if not ph.wait_for(lambda: tl.first("synth-end", "Beta") is not None, P - 0.2):
        print("  ! SETUP FAILURE — Beta was not prepared while Alpha played; "
              "the prefetch window under test does not exist on this service")
        print(f"  timeline: {tl.dump()}")
        return 2
    reply_token = svc._speak_token
    if reply_token is None:
        print("  ! SETUP FAILURE — no reply token published")
        return 2

    t_stop = tl.now()
    threading.Thread(target=svc.stop, daemon=True).start()
    t.join(timeout=20.0)
    if t.is_alive():
        print("  ! the worker never finished after the stop")
        return 2
    time.sleep(0.3)   # let any late frame or event land before asserting none

    synth_after = tl.after("synth-start", t_stop)
    play_after = tl.after("play-start", t_stop)
    beta_closed = tl.first("closed", "Beta") is not None
    beta_played = tl.first("play-start", "Beta") is not None
    alpha_cancel = tl.first("cancel", "Alpha") is not None
    leaked = svc._cancels.live()
    alpha_done = spy.completed("Alpha")
    beta_frames = spy.frames_for("Beta")

    print(f"  stop landed at t={t_stop:.3f}, Beta prepared at "
          f"t={tl.first('synth-end', 'Beta'):.3f} (before the stop)")
    print(f"  synthesis started after the stop: {synth_after or 'none'}")
    print(f"  playback started after the stop:  {play_after or 'none'}")
    print(f"  Beta (prefetched): closed={beta_closed} played={beta_played}")
    print(f"  Alpha observed the stop mid-playback: {alpha_cancel}")
    print(f"  reply token cancelled: {reply_token.cancelled}; leaked: {leaked or 'none'}")
    print(f"  subtitles — Alpha claims 100%: {alpha_done}; Beta frames: "
          f"{len(beta_frames)}")
    print(f"  timeline: {tl.dump()}")

    if not reply_token.cancelled or not alpha_cancel:
        print("  ! SETUP FAILURE — the stop did not reach the playing sentence")
        return 2

    bad = []
    if synth_after:
        bad.append(f"synthesis started after the stop: {synth_after}")
    if play_after:
        bad.append(f"audio started after the stop: {play_after}")
    if beta_played:
        bad.append("the prefetched sentence was PLAYED after the stop")
    if not beta_closed:
        bad.append("the prefetched sentence was never closed (connection held)")
    if leaked:
        bad.append(f"leaked tokens: {leaked}")
    if alpha_done:
        bad.append("Alpha's subtitle claimed 100% after being cut")
    if beta_frames:
        bad.append(f"{len(beta_frames)} subtitle frame(s) for a sentence never played")
    if bad:
        print("\nFAIL: the stop did not drop the prefetch —")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("\nPASS: stop after sentence 1 — no further synthesis, no audio after "
          "the stop, the prepared sentence closed unplayed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
