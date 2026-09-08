#!/usr/bin/env python3
"""Issue #154, the wider table: every token shape that puts a boundary at the
END of the buffer, plus the shapes that must NOT change.

Each row is one AI reply through the real worker. A row is red when the
spoken text is not the reply joined by single spaces (a cut that lost its
space, or a cut inside a word), when whitespace leaks into a TTS text, or
when the subtitle texts differ from the TTS texts. Measured on the merge
base 5dde4b4: 7 of 13 rows fuse -- not just the issue's ". " token, but any
buffer holding two boundaries and ending in [.!?] + whitespace (newline,
double space, "!" / "?", an ellipsis). The rows that pass there are the
guards: decimals, tokenizer-shaped tokens, fullwidth punctuation (not a
boundary today, not one after), and text with no terminal punctuation.

exit 0 = every row clean; 1 = a row fused; 2 = setup (a worker hung or a row
spoke nothing).
"""
import sys

import split_harness as sh

# (name, tokens). Names are what a red must print.
CASES = [
    ("issue: tokens end '. '",      ["Alpha one is here. ", "Beta two follows. ", "Gamma three ends."]),
    ("tokenizer-shaped",            ["Alpha one is here.", " Beta two follows.", " Gamma three ends."]),
    ("two sentences in one token",  ["Alpha one. Beta two. ", "Gamma three."]),
    ("boundary then newline",       ["Alpha one.\n", "Beta two.\n", "Gamma three."]),
    ("punct alone then space",      ["Alpha one", ".", " ", "Beta two", ".", " Gamma."]),
    ("space alone token",           ["Alpha one.", " ", "Beta two.", " ", "Gamma."]),
    ("decimal 3.5",                 ["Pi is about 3.", "5 today. ", "Really."]),
    ("abbrev e.g.",                 ["Use a tool, e.g. ", "a hammer. ", "Done."]),
    ("exclaim/question",            ["Wow! ", "Really? ", "Yes."]),
    ("ellipsis",                    ["Wait... ", "for it. ", "Now."]),
    ("unicode fullwidth",           ["日本語です。", "次の文。", "終わり"]),
    ("double trailing space",       ["Alpha one.  ", "Beta two.  ", "Gamma."]),
    ("no punctuation",              ["just some", " words with no end"]),
]


def main():
    print(f"service under test: {sh.SVC_PATH}")
    mod, _tl, _inj = sh.load()
    svc = sh.make_service(mod)

    failed, setup = [], []
    print(f"  {'row':28} {'':4} TTS texts")
    for name, tokens in CASES:
        r = sh.speak_reply(mod, svc, tokens)
        if not r.finished or not r.tts_texts:
            setup.append(name)
            print(f"  {name:28} !!   worker {'hung' if not r.finished else 'spoke nothing'}")
            continue
        bad = r.problems()
        if r.leaked:
            bad.append(f"leaked cancel tokens: {r.leaked}")
        tag = "ok" if not bad else "BAD"
        print(f"  {name:28} {tag:4} {r.tts_texts}")
        for b in bad:
            print(f"  {'':28}      - {b}")
        if bad:
            failed.append(name)

    if setup:
        print(f"\n!! SETUP FAILURE — {len(setup)} row(s) measured nothing: {setup}")
        return 2
    if failed:
        print(f"\n[FAIL] {len(failed)}/{len(CASES)} rows fused: {', '.join(failed)}")
        return 1
    print(f"\n[PASS] {len(CASES)}/{len(CASES)} rows: speech == reply joined by "
          "single spaces; subtitles match; no whitespace in TTS text.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
