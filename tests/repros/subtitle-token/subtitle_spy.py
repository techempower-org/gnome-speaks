"""Shared bits for the issue #42 subtitle-token repros.

Reuses ./.../cancel-tokens/harness.py verbatim (same stubs, no
audio, no network, no mic, no D-Bus, no port 7710, no ~/.config writes, no
service restart).  Since 2026-09-06 that harness isolates its scratch per PID
(/var/tmp/cancel-repros-<pid>) and repoints CONFIG_PATH at a temp file, so
concurrent runs no longer contaminate each other -- re-verified on both sides
after that rewrite, identical frame sequences.  What this file adds is the one
instrument the cancel-token repros did not need: a way to see the
SubtitleUpdate frames.

_run_subtitle_progress publishes every frame through GLib.idle_add, and the
harness runs no GLib main loop, so those callbacks are queued and never
executed -- the frames are invisible.  GLibSpy proxies the module's GLib and
intercepts exactly the _emit_subtitle_update calls, recording (text, percent).
Every other GLib call is forwarded to the real GLib untouched, so the service
behaves as it does under the other suites.

GLibSpy can also STALL one frame's idle_add.  That is not a trick to make a bug
appear: publishing a subtitle frame to a busy GNOME Shell main loop really can
block the emitting thread, and the stall is how a repro pins a real but narrow
window to a deterministic width -- the same discipline repro_a uses when it
makes the already-stubbed _schedule_warmup take 250 ms.
"""
import atexit
import contextlib
import inspect
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "cancel-tokens"))

import harness  # noqa: E402  (path must be set first)

SVC_PATH = harness.SVC_PATH


class GLibSpy:
    """Proxy for the service module's GLib that records subtitle frames.

    ⭐ THIS IS AN INSTRUMENT, NOT A CONVENIENCE — do not drop it in a refactor
    or a suite move.  _run_subtitle_progress publishes every frame through
    GLib.idle_add, and these repros run no GLib main loop, so those callbacks
    are queued and NEVER EXECUTED.  A subtitle assertion written without this
    shim reads an empty list, measures nothing, and PASSES.  That is a green
    test with no writer behind it — the failure mode that hides the bug it was
    written to catch.

    Everything except _emit_subtitle_update is forwarded to the real GLib
    untouched, so the service under test behaves exactly as it does under the
    other suites.
    """

    def __init__(self, real, state_mod):
        self._real = real
        self._state = state_mod
        self.frames = []                 # [(text, percent, t)]
        self.t0 = time.monotonic()
        self._arm = threading.Event()    # stall the next frame
        self.stalling = threading.Event()
        self.stall_done = threading.Event()
        self.stall_seconds = 0.0
        self.wire_at_stall_exit = None   # state._cancel_event when the stall ended

    def __getattr__(self, name):         # everything else is real GLib
        return getattr(self._real, name)

    def arm_stall(self, seconds):
        self.stall_seconds = seconds
        self.stall_done.clear()
        self.stalling.clear()
        self._arm.set()

    def idle_add(self, fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "_emit_subtitle_update":
            text, _dur, pct = args
            self.frames.append((text, pct, time.monotonic() - self.t0))
            if self._arm.is_set():
                self._arm.clear()
                self.stalling.set()
                time.sleep(self.stall_seconds)
                # Read the wire at the moment the thread regains control --
                # this is what the buggy final check is about to read.
                self.wire_at_stall_exit = self._state._cancel_event.is_set()
                self.stall_done.set()
            return 0
        return self._real.idle_add(fn, *args, **kwargs)

    # -- assertions --------------------------------------------------------
    def frames_for(self, prefix):
        return [f for f in self.frames if f[0].startswith(prefix)]

    def completed(self, prefix):
        """Did a 100% 'this utterance finished' frame go out for prefix?"""
        return any(pct == 100 for _t, pct, _ts in self.frames_for(prefix))


ISOLATION_PINS = {
    # Values this suite's assertions depend on. terminal_mode is the sharp one:
    # use_lexical rewrites transcripts before they are asserted on, and JP's
    # live config really does carry terminal_mode=True sometimes.
    "terminal_mode": False,
    "chronicle": False,
    "wake_word": False,
    "key": "test-key",
}


def assert_isolated(mod):
    """Prove the harness's isolation is in effect before asserting anything.

    The path checks (CHRONICLE_PATH, CONFIG_PATH, XDG_STATE_HOME) belong to the
    harness and are strictly stronger than the copy this file used to carry --
    CHRONICLE_PATH in particular is computed at module import, which no check
    living out here could see.  Delegate to it rather than maintain a second,
    weaker version that will drift.

    What stays here is the other half: proving the CONFIG pins actually TOOK.
    A path can be redirected correctly while a pin is silently missing, and
    terminal_mode is the one that matters -- use_lexical rewrites transcripts
    before they are asserted on, and JP's live config really does carry
    terminal_mode=True sometimes.

    Raises SystemExit(2) -- a SETUP FAILURE, never a pass and never a bug.
    """
    harness.assert_isolated(mod)          # paths; raises on its own
    problems = [f"CONFIG[{k!r}] is {mod.CONFIG.get(k)!r}, expected {want!r}"
                for k, want in ISOLATION_PINS.items()
                if mod.CONFIG.get(k) != want]
    if problems:
        print("  ! SETUP FAILURE — harness CONFIG pins are not in effect:")
        for pr in problems:
            print(f"      - {pr}")
        raise SystemExit(2)
    return True


def scenario(mod, **overrides):
    """Per-scenario CONFIG for a single-scenario script, via config_scope.

    The swept harness owns per-scenario flags now.  These repros are one
    scenario per process, so the `with` block would wrap the whole body and
    buy nothing but an indent; entering the context and closing it at exit
    keeps the restore semantics without pretending there is a second scenario
    to protect.
    """
    stack = contextlib.ExitStack()
    stack.enter_context(harness.config_scope(mod, **overrides))
    atexit.register(stack.close)
    return mod.CONFIG


def install_spy(mod):
    spy = GLibSpy(mod.GLib, mod.state)
    mod.GLib = spy
    return spy


def subtitle_takes_token(mod):
    """True when _run_subtitle_progress carries its utterance's verdict.

    Lets one script run against both origin/main (3 params) and the fix
    (4 params) instead of two divergent copies.
    """
    params = inspect.signature(mod.GnomeSpeaksService._run_subtitle_progress).parameters
    return "token" in params


def run_subtitle_progress(mod, svc, text, est, stop_event, token):
    if subtitle_takes_token(mod):
        return svc._run_subtitle_progress(text, est, stop_event, token)
    return svc._run_subtitle_progress(text, est, stop_event)
