#!/usr/bin/env python3
"""Issue #154: a token ending ". " must not fuse the next sentence onto it.

_split_sentences returned the LAST re.split part as the leftover buffer, but
the split had already consumed the whitespace that followed it. So with the
stream the issue measured --

    "Alpha one is here. " | "Beta two follows. " | "Gamma three ends."

-- the leftover after the second token was "Beta two follows." (space gone)
and the third token was appended flush: "Beta two follows.Gamma three ends.",
spoken as ONE sentence, subtitled as one, timed as one.

This drives the real worker with exactly those tokens and asserts:
  (a) three synthesis calls, one per sentence, in order
  (b) each text is its own strip() -- no boundary whitespace leaked into TTS
  (c) the spoken text is the reply joined by single spaces
  (d) the subtitle overlay saw the same three texts

exit 0 = three sentences; 1 = fused (the defect); 2 = setup.
"""
import sys

import split_harness as sh

TOKENS = ["Alpha one is here. ", "Beta two follows. ", "Gamma three ends."]
WANT = ["Alpha one is here.", "Beta two follows.", "Gamma three ends."]


def main():
    print(f"service under test: {sh.SVC_PATH}")
    mod, _tl, _inj = sh.load()
    svc = sh.make_service(mod)

    r = sh.speak_reply(mod, svc, TOKENS)
    if not r.finished:
        print("  ! SETUP FAILURE — the worker never finished")
        return 2
    if not r.tts_texts:
        print("  ! SETUP FAILURE — nothing reached synthesis; nothing measured")
        return 2

    print(f"  tokens:    {TOKENS}")
    print(f"  TTS texts: {r.tts_texts}")
    print(f"  subtitles: {r.subtitle_texts}")
    bad = r.problems()
    if r.tts_texts != WANT:
        bad.insert(0, f"{len(r.tts_texts)} synthesis call(s), expected 3: "
                      f"{r.tts_texts!r}")
    if r.leaked:
        bad.append(f"leaked cancel tokens: {r.leaked}")
    if bad:
        print("\nFAIL: the ' . ' boundary was lost —")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("\nPASS: three sentences, three synthesis calls, subtitles match, "
          "no whitespace in any TTS text.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
