"""Harness for the issue #134 repros: a TWO-PHASE fake TTS.

Reuses the hardened pieces of the other suites instead of forking them:
  * cancel-tokens/harness.py  -- per-PID scratch, _isolate_config, make_service,
    wait_for, the RecordingInjector.
  * subtitle-token/subtitle_spy.py -- GLibSpy (no GLib main loop runs here, so
    SubtitleUpdate frames are otherwise invisible) and assert_isolated.
  * begin-refused/shim.py -- stop_in_the_claim_window, record_states.

What this file adds is the instrument the others could not have: a fake
`speech_tts` whose SYNTHESIS and PLAYBACK are separate, timed phases, so the
question "did sentence N+1's synthesis start before sentence N's playback
ended?" has a writer behind it.

    tts_prepare(text, **kw)   -> sleeps SYNTH s     ("synth-start", "synth-end")
    tts_play(handle, ...)     -> waits PLAY s       ("play-start", "play-end" | "cancel")
    handle.close()            ->                    ("closed")
    tts(text, **kw)           -> prepare + play, serially, same events

`tts()` is installed too, and records the SAME event kinds, so the baseline
service (which only knows `tts()`) produces a comparable timeline: there,
every synth-start necessarily follows the previous play-end.  On the fixed
service the AI-reply path resolves tts_prepare/tts_play and prepares ahead.

Playback honours the global wire (state._cancel_event) exactly like the real
speech_tts.tts does -- that is the seam a stop actually reaches.

exit codes follow the repo contract: 0 clean, 1 defect present, 2 SETUP
FAILURE (the window under test was never created -- on the pre-#134 baseline
the "prepared ahead" window does not exist, so p2 and p3 report 2 there, not
0: they measure nothing about a service that never prefetches).
"""
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _d in ("cancel-tokens", "subtitle-token", "begin-refused"):
    sys.path.insert(0, os.path.join(_ROOT, _d))

import harness            # noqa: E402
import subtitle_spy       # noqa: E402
import shim               # noqa: E402  (begin-refused)

SVC_PATH = harness.SVC_PATH
make_service = harness.make_service
wait_for = harness.wait_for
stop_in_the_claim_window = shim.stop_in_the_claim_window
record_states = shim.record_states


class Timeline:
    """Ordered (kind, tag, t) events from the fake, plus the queries the
    repros ask of them. `tag` is the sentence's first word."""

    def __init__(self):
        self.events = []
        self.t0 = time.monotonic()
        self._lock = threading.Lock()

    def now(self):
        return time.monotonic() - self.t0

    def add(self, kind, tag):
        with self._lock:
            self.events.append((kind, tag, self.now()))

    def first(self, kind, tag):
        for k, g, t in list(self.events):
            if k == kind and g == tag:
                return t
        return None

    def of_kind(self, kind):
        return [(g, t) for k, g, t in list(self.events) if k == kind]

    def after(self, kind, t_cut):
        return [(g, t) for k, g, t in list(self.events)
                if k == kind and t > t_cut]

    def dump(self):
        return [(k, g, round(t, 3)) for k, g, t in list(self.events)]


class _Handle:
    """What the fake tts_prepare returns: the prepared-but-unplayed sentence."""

    __slots__ = ("text", "tag", "closed", "played", "_tl")

    def __init__(self, text, tl):
        self.text = text
        self.tag = text.split()[0]
        self.closed = False
        self.played = False
        self._tl = tl

    def close(self):
        if not self.closed:
            self.closed = True
            self._tl.add("closed", self.tag)


def load(synth_seconds=0.4, play_seconds=0.6):
    """Import the service with the two-phase fake installed.

    Returns (mod, timeline, injector). `mod.speech_tts` carries tts_prepare,
    tts_play AND tts -- the fixed service takes the first two, the baseline
    takes the last, and both leave the same event kinds in the timeline.
    """
    mod, _events, inj = harness.load(fake_tts_seconds=play_seconds)
    tl = Timeline()

    def _play(handle, **kw):
        # The tag gates on identity, not just text: a handle that was
        # closed must be reported as such if anything ever plays it.
        if handle.closed:
            tl.add("played-after-close", handle.tag)
        handle.played = True
        tl.add("play-start", handle.tag)
        deadline = time.monotonic() + play_seconds
        while time.monotonic() < deadline:
            if mod.state._cancel_event.is_set():
                tl.add("cancel", handle.tag)
                return {"spoken": False, "cancelled": True}
            time.sleep(0.005)
        tl.add("play-end", handle.tag)
        return {"spoken": True}

    def tts_prepare(text, **kw):
        h = _Handle(text, tl)
        tl.add("synth-start", h.tag)
        time.sleep(synth_seconds)          # Azure RTT / Piper first chunk
        tl.add("synth-end", h.tag)
        return h

    def tts_play(handle, **kw):
        return _play(handle, **kw)

    def tts(text, **kw):
        return _play(tts_prepare(text), **kw)

    mod.speech_tts.tts_prepare = tts_prepare
    mod.speech_tts.tts_play = tts_play
    mod.speech_tts.tts = tts
    return mod, tl, inj


def run_reply(mod, svc, tokens, timeout=30.0):
    """Drive _conversation_worker with a fake LLM stream; join it."""
    mod.stream_chat = lambda **kw: iter(list(tokens))
    t = threading.Thread(target=svc._conversation_worker,
                         args=("hello",), daemon=True)
    t.start()
    return t
