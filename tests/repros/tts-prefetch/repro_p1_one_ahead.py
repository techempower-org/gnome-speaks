#!/usr/bin/env python3
"""Issue #134: sentence N+1's synthesis must START before sentence N's playback ENDS.

The AI-reply loop called speech_tts.tts(sentence) synchronously per sentence,
and tts() synthesizes AND plays before returning -- so N+1 was not even
requested until N had finished playing, and every sentence boundary paid the
full synthesis round trip as silence.  With a fake whose synthesis takes S and
playback takes P, three sentences cost:

    serial (baseline)        3 * (S + P)
    one ahead (fixed)        S + 3 * P        (N+1 synthesized under N)

This repro measures both facts on the real worker, with the real sentence
splitter, and prints the table.  Three assertions, in order of how directly
they state the defect:

  (a) synth-start(Beta)  <  play-end(Alpha)      -- the issue's own assertion
  (b) synth-start(Gamma) >= play-start(Beta)     -- ONE ahead, not two: a second
                                                    synthesis never starts until
                                                    the prepared one is playing
  (c) wall(first synth-start .. last play-end) < 3(S+P) - P/2

exit 0 = pipelined; 1 = serial (the defect); 2 = setup.
"""
import sys

import prefetch_harness as ph

S, P = 0.4, 0.6
# Tokenizer-shaped: punctuation ends a token, the SPACE opens the next one.
# A token ending ". " trips a separate, pre-existing _split_sentences bug
# (the boundary whitespace is consumed and the next token fuses onto the
# sentence: "follows.Gamma") -- filed on its own; this suite measures #134.
TOKENS = ["Alpha one is here.", " Beta two follows.", " Gamma three ends."]


def main():
    print(f"service under test: {ph.SVC_PATH}")
    mod, tl, _inj = ph.load(synth_seconds=S, play_seconds=P)
    ph.subtitle_spy.assert_isolated(mod)
    svc = ph.make_service(mod)
    ph.subtitle_spy.scenario(mod, conversation_mode=True)

    t = ph.run_reply(mod, svc, TOKENS)
    t.join(timeout=30.0)
    if t.is_alive():
        print("  ! the worker never finished")
        return 2

    starts = tl.of_kind("play-start")
    ends = tl.of_kind("play-end")
    if [g for g, _ in starts] != ["Alpha", "Beta", "Gamma"] or len(ends) != 3:
        print(f"  ! SETUP FAILURE — expected three sentences played in order, "
              f"got starts={starts} ends={ends}")
        print(f"  timeline: {tl.dump()}")
        return 2
    if svc._cancels.live():
        print(f"  ! leaked tokens: {svc._cancels.live()}")
        return 1

    sa_b = tl.first("synth-start", "Beta")
    pe_a = tl.first("play-end", "Alpha")
    sa_g = tl.first("synth-start", "Gamma")
    ps_b = tl.first("play-start", "Beta")
    wall = tl.first("play-end", "Gamma") - tl.first("synth-start", "Alpha")
    serial = 3 * (S + P)
    pipelined = S + 3 * P

    print(f"  S (synthesis) = {S:.2f}s   P (playback) = {P:.2f}s   3 sentences")
    print(f"  {'':22} {'predicted':>10} {'measured':>10}")
    print(f"  {'serial   3(S+P)':22} {serial:>10.2f}")
    print(f"  {'one-ahead S+3P':22} {pipelined:>10.2f}")
    print(f"  {'wall, this service':22} {'':>10} {wall:>10.2f}")
    print(f"  (a) synth-start(Beta) {sa_b:.3f}  vs  play-end(Alpha) {pe_a:.3f}"
          f"  -> {'BEFORE (prefetched)' if sa_b < pe_a else 'AFTER (serial)'}")
    print(f"  (b) synth-start(Gamma) {sa_g:.3f}  vs  play-start(Beta) {ps_b:.3f}"
          f"  -> {'one ahead' if sa_g >= ps_b - 0.005 else 'TWO ahead'}")
    print(f"  timeline: {tl.dump()}")

    bad = []
    if not sa_b < pe_a:
        bad.append("Beta's synthesis started only after Alpha finished playing "
                   "— every boundary pays the synthesis round trip")
    if sa_g < ps_b - 0.005:
        bad.append("Gamma's synthesis started before Beta began playing — "
                   "two sentences in flight, the bound is one")
    if not wall < serial - P / 2:
        bad.append(f"wall {wall:.2f}s is not measurably under the serial "
                   f"{serial:.2f}s")
    if bad:
        print("\nFAIL: the AI reply is not pipelined —")
        for b in bad:
            print(f"  - {b}")
        return 1
    print(f"\nPASS: one sentence synthesized ahead — wall {wall:.2f}s "
          f"(serial would be {serial:.2f}s, ideal {pipelined:.2f}s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
