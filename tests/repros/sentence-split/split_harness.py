"""Harness for the issue #154 repros: WHAT TEXT did the AI reply speak?

Builds on tts-prefetch/prefetch_harness.py (the real worker, a two-phase fake
speech_tts, per-PID scratch, isolated CONFIG) and adds the one instrument this
question needs: the fake records the FULL TEXT handed to synthesis, not just
the first word. subtitle-token's GLibSpy records the subtitle frames, so the
text that reached the subtitle overlay is measured alongside, not inferred
from the fact that both come from the same variable.

speak_reply() drives _conversation_worker with a token list and returns a
Reply: the TTS texts in synthesis order, the distinct subtitle texts in frame
order, and the reply the worker would have chronicled ("".join(tokens)).

The invariant the repros assert, stated once here:

    " ".join(tts_texts) == " ".join(reply.split())

i.e. the streamed speech is the whole reply with exactly one space between
sentences -- no characters lost, no split inside a word, and no boundary
whitespace leaked into a sentence (each text must equal its own strip()).
It holds however the splitter chooses to cut, so a row cannot go red for
splitting "e.g." on its own (which today's splitter does, and this fix does
not make worse); it goes red only when a cut loses the space it stood on.

exit codes follow the repo contract: 0 clean, 1 defect present, 2 SETUP
FAILURE (the worker never finished / spoke nothing -- nothing measured).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "tts-prefetch"))

import prefetch_harness as ph   # noqa: E402

SVC_PATH = ph.SVC_PATH
wait_for = ph.wait_for

# Fast: the question is WHAT was spoken, not WHEN. p1 owns the timing.
SYNTH, PLAY = 0.02, 0.03


class Reply:
    def __init__(self, tokens):
        self.tokens = list(tokens)
        self.reply = "".join(self.tokens)
        self.expected = " ".join(self.reply.split())
        self.tts_texts = []
        self.subtitle_texts = []
        self.finished = False
        self.leaked = []

    @property
    def spoken(self):
        return " ".join(self.tts_texts)

    def problems(self):
        """Every way this reply's speech differs from the reply. [] = clean."""
        bad = []
        if self.spoken != self.expected:
            bad.append(f"spoken {self.spoken!r} != reply {self.expected!r}")
        for t in self.tts_texts:
            if t != t.strip() or not t:
                bad.append(f"whitespace leaked into TTS text {t!r}")
        if self.subtitle_texts != self.tts_texts:
            bad.append(f"subtitles {self.subtitle_texts!r} != TTS {self.tts_texts!r}")
        return bad


def load():
    """Import the service with the recording two-phase fake and the GLib spy."""
    mod, tl, inj = ph.load(synth_seconds=SYNTH, play_seconds=PLAY)
    ph.subtitle_spy.assert_isolated(mod)
    spy = ph.subtitle_spy.install_spy(mod)
    texts = []
    prepare, play = mod.speech_tts.tts_prepare, mod.speech_tts.tts_play

    def rec_prepare(text, **kw):
        texts.append(text)
        return prepare(text, **kw)

    mod.speech_tts.tts_prepare = rec_prepare
    # An older service (or a stubbed seam) takes tts(); record the same way.
    mod.speech_tts.tts = lambda text, **kw: play(rec_prepare(text, **kw), **kw)
    mod._154_texts = texts
    mod._154_spy = spy
    return mod, tl, inj


def make_service(mod):
    svc = ph.make_service(mod)
    ph.subtitle_spy.scenario(mod, conversation_mode=True)
    return svc


def speak_reply(mod, svc, tokens, timeout=30.0):
    """One AI reply through the real worker. Returns a Reply."""
    r = Reply(tokens)
    texts, spy = mod._154_texts, mod._154_spy
    n_texts, n_frames = len(texts), len(spy.frames)
    t = ph.run_reply(mod, svc, r.tokens)
    t.join(timeout=timeout)
    r.finished = not t.is_alive()
    if not r.finished:
        return r
    # The speaker is joined before the worker returns, and the 100 % frame is
    # emitted before the subtitle worker exits -- but its thread is joined
    # with a 2 s timeout, so give the last frame a moment to land.
    wait_for(lambda: svc.current_state == "idle", 5.0)
    frames = [f[0] for f in spy.frames[n_frames:]]
    r.tts_texts = list(texts[n_texts:])
    for f in frames:                      # distinct, in first-seen order
        if not r.subtitle_texts or r.subtitle_texts[-1] != f:
            r.subtitle_texts.append(f)
    r.leaked = list(svc._cancels.live())
    return r
