"""Shared isolation core for the repro harnesses (#90).

Five harnesses carried a byte-identical copy of this (three of them literally
byte-identical; chronicle-perf differed by ONE character, the `chronicle` pin).
That was the right shape in scratch, where every suite had to be self-contained
and copyable, and the wrong shape in a versioned tree: a correction to the
isolation contract had to be made five times, and copies drift.

Two external inputs used to decide these repros' verdicts, and both produced
results that read as product regressions. Everything here exists for one of
them.

1. JP's LIVE ~/.config/speech-to-cli/config.json. state.load_config() reads it
   at import, so CONFIG started as ~47 live desktop keys. Worse,
   _reload_config_flags() RE-READS that file mid-run and overwrites CONFIG for
   every key in _SYNC_FLAGS, so a pin applied after load() is silently
   clobbered unless the repro also stubs that method; and _save_config_flag()
   WRITES it.
2. Another agent running the same suite. Every scratch path used to be a fixed
   absolute directory, so two processes shared one chronicle file. MEASURED:
   two concurrent runs of verify_chronicle_contract.py both fail with
   'equivalence [...]' mismatches; each alone passes.

NOT moved in here, deliberately:

  * subtitle_spy.assert_isolated() delegates its PATH checks to the harness's
    and adds the other half -- that the CONFIG pins actually TOOK. A path can
    be redirected correctly while a pin is silently missing, and terminal_mode
    really is True on the developer desktop. Both halves are needed and they
    answer different questions.
  * prefs-rig is GJS/bash and shares none of this. It must never be imported
    by a python collector, and its exit contract is its own.
"""
import atexit
import contextlib
import glob
import json
import os
import re
import shutil

# Every key here is pinned because some repro's verdict depends on it. Nothing
# may be read from the live config -- see the module docstring.
CONFIG_PINS = {
    "key": "test-key",              # speak() bails out early without one
    "wake_word": False,             # the wake watcher must never open the mic
    "wake_word_model": "",
    "wake_word_secure_gate": False, # #55: gates live_typing for the session
    "continuous_dictation": False,  # is_loop -- picks the whole cycle shape
    "conversation_mode": False,
    "dictation_mode": True,
    "terminal_mode": False,         # use_lexical: rewrites text before asserts
    "skip_final_paste": False,      # picks finalize() vs paste() in step 9
    "injection_method": "ydotool",
    "read_notifications": False,
    "llm_thinking": False,
    "spiel_provider": False,        # a second D-Bus name on the session bus
    "debug": False,                 # else every run appends to /tmp/speech-debug.log
    "live_subtitles": False,
    "phrase_list": [],
    "wyoming_host": "",             # no LAN fallback from a test
    "chronicle": False,             # chronicle-perf overrides this to True
}

REAL_STATE = os.path.join(os.path.expanduser("~/.local/state"), "gnome-speaks")


def setup_scratch(prefix, scratch_root):
    """Choose this run's scratch dir and point XDG_STATE_HOME at it.

    MUST be called at harness import, before the service module is exec'd:
    CHRONICLE_PATH is computed at MODULE IMPORT from $XDG_STATE_HOME, so this
    is one moved import away from silently attaching a repro to the real
    ledger.

    GS_REPRO_SCRATCH is read with get(), never setdefault -- setdefault EXPORTS
    the path, so a child process would inherit the PARENT's dir and its atexit
    would delete the still-running parent's scratch. Reset and cleanup are
    therefore gated on this process having CHOSEN the directory: a caller-pinned
    path belongs to the caller.

    `prefix` is the suite's historical directory prefix and is deliberately not
    derived: version-cache uses "version-cache", which does not match the
    "*-repros-*" sweep glob below and so has never been swept. Deriving it
    would silently change that.

    Returns (scratch, state_dir).
    """
    pinned = os.environ.get("GS_REPRO_SCRATCH")
    scratch = pinned or os.path.join(scratch_root, f"{prefix}-{os.getpid()}")
    state_dir = os.path.join(scratch, "state")
    os.environ["XDG_STATE_HOME"] = state_dir
    if pinned is None:
        # A verdict must never depend on who ran here before.
        shutil.rmtree(scratch, ignore_errors=True)
        atexit.register(shutil.rmtree, scratch, True)
    os.makedirs(state_dir, exist_ok=True)
    os.environ.setdefault("SPEECH_ENGINE_PATH",
                          os.path.expanduser("~/Projects/speech-to-cli"))
    return scratch, state_dir


def sweep_stale_scratch(scratch):
    """Remove sibling scratch dirs whose owning process is gone.

    atexit does NOT run when `timeout` SIGTERMs a run, which is how these are
    usually killed, so PID-keyed dirs accumulate. Only a dir whose PID no
    longer exists is touched, so a live sibling run is never swept out from
    under itself -- a leftover directory is not evidence of a leak until its
    PID has been checked dead.
    """
    for d in glob.glob(os.path.join(os.path.dirname(scratch), "*-repros-*")):
        m = re.search(r"-repros-(\d+)$", d)
        if not m or d == scratch:
            continue
        try:
            os.kill(int(m.group(1)), 0)      # signal 0: liveness probe only
        except ProcessLookupError:
            shutil.rmtree(d, ignore_errors=True)
        except PermissionError:
            pass                              # someone else's live process
        except Exception:
            pass


def isolate_config(mod, scratch, pins=None):
    """Rebuild CONFIG from the service's OWN defaults plus `pins`.

    CONFIG is mutated IN PLACE: `from state import CONFIG` means the service,
    audio, stt, speech_tts and wyoming all hold the same dict object, so
    rebinding mod.CONFIG would isolate the service module and miss every other
    one. CONFIG_PATH is repointed at a file holding the same values, so
    _reload_config_flags() re-reading it mid-run is a no-op that re-applies our
    pins, and _save_config_flag() can never touch the real file.
    """
    pins = CONFIG_PINS if pins is None else pins
    real_defaults = mod.state.DEFAULTS_PATH
    try:
        mod.state.DEFAULTS_PATH = os.path.join(scratch, "absent-config.json")
        baseline = mod.state.load_config()
    finally:
        mod.state.DEFAULTS_PATH = real_defaults
    baseline.update(pins)
    path = os.path.join(scratch, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(baseline, f)
    mod.CONFIG_PATH = path
    mod.CONFIG.clear()
    mod.CONFIG.update(baseline)
    return baseline


def assert_isolated(mod, scratch):
    """Prove the module cannot reach the live state, BEFORE any scenario.

    Raises rather than warns: a repro that has quietly attached itself to the
    real ledger must not be allowed to report a verdict.
    """
    root = os.path.realpath(scratch)
    checks = {
        "CHRONICLE_PATH": getattr(mod, "CHRONICLE_PATH", None),
        "CONFIG_PATH": getattr(mod, "CONFIG_PATH", None),
        "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME"),
    }
    for name, value in checks.items():
        if value is None:
            raise AssertionError(f"{name} is unset -- cannot prove isolation")
        real = os.path.realpath(value)
        if not (real == root or real.startswith(root + os.sep)):
            raise AssertionError(
                f"{name}={value!r} resolves OUTSIDE the isolated scratch "
                f"{scratch!r} -- refusing to run")
        if os.path.realpath(REAL_STATE) in (real, os.path.dirname(real)):
            raise AssertionError(f"{name} points at the live state dir")
    return True


@contextlib.contextmanager
def config_scope(mod, **overrides):
    """Run ONE scenario with its own CONFIG flags, restored afterwards.

    Every load() in a process hands back the SAME dict, so two scenarios in one
    process silently clobber each other's flags and the second one's verdict
    describes the first one's configuration.

    Three traps a scenario can hit that produce a GREEN run while measuring the
    WRONG code path -- all three found by lucid-pin-lifecycle:

    1. `_reload_config_flags()` restores conversation_mode=False at
       start_listening(), so a conversation-mode scenario silently takes the
       DICTATION path, the reply path never runs, and the repro still prints a
       paste and looks like it passed. The CONFIG_PATH redirect closes this,
       but only for flags that are IN the pins. Put a scenario's flags here or
       in CONFIG_PINS, never in a bare CONFIG[...] assignment after load().
    2. In SINGLE-SHOT, turn_end sets _stop_event and
       _stream_conversation_worker aborts on it before stream_chat is ever
       called. Any reply-path scenario needs loop mode, and should assert
       stream_chat was actually invoked.
    3. Production calls _conversation_worker SYNCHRONOUSLY from inside
       _streaming_stt_cycle, so the cycle's `finally` -- and its injector
       release -- runs after the reply. A scenario that starts the worker on
       its OWN thread has no such release and will report a stranded backend
       that production does not have.
    """
    saved = dict(mod.CONFIG)
    mod.CONFIG.update(overrides)
    try:
        yield mod.CONFIG
    finally:
        mod.CONFIG.clear()
        mod.CONFIG.update(saved)
