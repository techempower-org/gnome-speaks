"""Path shim for the issue #79 repros.

Reuses two existing, already-hardened pieces instead of forking them:
  * cancel-tokens/harness.py -- per-PID scratch, _isolate_config,
    the fake TTS that honours the global wire, the RecordingInjector.
  * lucid-subtitle-token-repros/subtitle_spy.py -- GLibSpy (the harness runs no
    GLib main loop, so SubtitleUpdate frames are otherwise invisible) and
    assert_isolated (runtime proof the config pins are in effect).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _d in ("cancel-tokens", "subtitle-token"):
    sys.path.insert(0, os.path.join(_ROOT, _d))

import harness            # noqa: E402
import subtitle_spy       # noqa: E402

SVC_PATH = harness.SVC_PATH


def stop_in_the_claim_window(svc, label="ai-reply"):
    """Make a stop land between issue() and begin() for the next `label`.

    That window is real and narrow: _stream_conversation_worker issues the
    reply's token, publishes it as _speak_token, and only then calls begin().
    A stop arriving in between is exactly what begin() returning False MEANS.
    Rather than race for it, hook issue() so the cancel happens inside the
    window every time.

    The cancel is the registry call stop() itself makes (cancel_all writes a
    verdict on every live token, then raises the wire); going through the
    registry rather than svc.stop() avoids the worker thread joining itself.

    Returns a dict with 'fired' so the repro can prove the window was hit
    instead of assuming it.
    """
    state = {"fired": False, "token": None}
    real_issue = svc._cancels.issue

    def hooked(lbl):
        token = real_issue(lbl)
        if lbl == label and not state["fired"]:
            state["fired"] = True
            state["token"] = token
            svc._cancels.cancel_all()
        return token

    svc._cancels.issue = hooked
    return state


def record_states(svc):
    """Capture every _set_state transition, in order.

    The badge is a third half that has to agree with the other two: a reply
    that is never spoken must never announce "speaking". Reading the FINAL
    state cannot see a flicker, so record the transitions instead.
    """
    seen = []
    real = svc._set_state

    def spy(state, *a, **kw):
        seen.append(state)
        return real(state, *a, **kw)

    svc._set_state = spy
    return seen
