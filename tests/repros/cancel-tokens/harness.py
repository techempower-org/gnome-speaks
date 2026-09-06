"""Shared harness for the cancel-token repros (issue #21).

Same discipline as ../service-audit/harness.py: import the real
gnome-speaks-service.py in-process with every dangerous seam stubbed.  No
audio, no network, no mic, no D-Bus, no port 7710, no ~/.config writes, no
service restart.

Differences from the audit harness:
  * scratch lives in the repo's gitignored tmp/, keyed by PID (never /tmp,
    which is a RAM-backed tmpfs on this workstation)
  * get_injector() is replaced by a RecordingInjector, so "did a transcript
    reach the cursor?" is a fact we can assert on instead of a stub no-op
  * the fake TTS honours the *global* wire (state._cancel_event) exactly like
    speech_tts.tts does -- the whole point is that the wire is shared

Run any repro with:
    GS_SVC_PATH=<path to a gnome-speaks-service.py> python3 <script>
exit 0 = clean, exit 1 = bug present.
"""
import atexit
import contextlib
import glob
import importlib.util
import json
import os
import shutil
import sys
import time

# ---------------------------------------------------------------------------
# ISOLATION -- read this before changing any path or CONFIG line below.
#
# Two external inputs used to decide these repros' verdicts, and both produced
# results that read as product regressions:
#
# 1. JP's LIVE ~/.config/speech-to-cli/config.json.  state.load_config() reads
#    it at import, so CONFIG started as ~47 live desktop keys (terminal_mode
#    and wake_word are True on this machine right now).  Worse,
#    _reload_config_flags() RE-READS that file mid-run and overwrites CONFIG
#    for every key in _SYNC_FLAGS, so a pin applied after load() is silently
#    clobbered unless the repro also stubs that method; and _save_config_flag()
#    WRITES it.  _isolate_config() below cuts all three wires.
#
# 2. Another agent running these same suites.  Every scratch path used to be a
#    fixed absolute directory, so two processes shared one chronicle file.
#    MEASURED: two concurrent runs of verify_chronicle_contract.py both fail
#    with 'equivalence [...]' mismatches; each alone passes.  Hence SCRATCH is
#    keyed by PID and removed at exit.
#
# CONCURRENCY, precisely (an earlier note here overstated this):
#   * verify_ibus_injector.py rmtree()s a FIXED runtime dir at import, so two
#     concurrent runs OF IT destroy each other's breadcrumb -- that is the
#     mechanism behind the one '[9] GetGlobalEngine raises' flake observed on
#     2026-09-06, not an interaction with verify_wake_gate.py.  Now per-PID.
#   * UNVERIFIED, flagged: the in-repo verify_wake_gate.py imports
#     ibus_injector WITHOUT redirecting XDG_RUNTIME_DIR, so anything in it that
#     reaches acquire() would write JP's REAL prior-engine breadcrumb -- which
#     is also what verify_ibus_injector's check [12] asserts must not exist.
# ---------------------------------------------------------------------------

# ENV CONTRACT (unified 2026-09-06): GS_SVC_PATH is the ONE input -- the
# service.py file under test. The worktree dir is DERIVED from its dirname,
# so sibling modules (spellbook, injector, ibus_injector) always come from
# the same tree as the service. GS_WT overrides the dir only if you really
# mean to mix trees. Defaults to the main checkout; the old defaults pointed
# at ~/Projects/gnome-speaks-wt/<name>/ worktrees that no longer exist, so a
# bare run died with FileNotFoundError instead of testing anything.
# Repo-relative by construction: this file lives at
# tests/repros/<suite>/<file>.py, so four dirnames reach the repo root. #59:
# the old defaults were absolute paths into ~/Projects/gnome-speaks-wt/<name>/
# worktrees that no longer existed, so a bare run died with FileNotFoundError
# instead of testing anything.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SVC_DEFAULT = os.path.join(REPO_ROOT, "gnome-speaks-service.py")
# Scratch lives in the repo's gitignored tmp/, keyed by PID. Never a fixed
# shared path: two agents running a suite at once used to corrupt each other
# and it read exactly like a service regression.
SCRATCH_ROOT = os.path.join(REPO_ROOT, "tmp", "repros")

SVC_PATH = os.environ.get("GS_SVC_PATH", SVC_DEFAULT)
WT = os.environ.get("GS_WT") or os.path.dirname(os.path.abspath(SVC_PATH))

# get(), never setdefault(): setdefault EXPORTS the path, so any child python
# this suite spawns would inherit the PARENT's dir and its atexit would delete
# the still-running parent's scratch out from under it.
_PINNED = os.environ.get("GS_REPRO_SCRATCH")
SCRATCH = _PINNED or os.path.join(SCRATCH_ROOT, f"cancel-repros-{os.getpid()}")
STATE_DIR = os.path.join(SCRATCH, "state")
os.environ["XDG_STATE_HOME"] = STATE_DIR        # BEFORE the module computes
                                                # CHRONICLE_PATH from it
if _PINNED is None:
    # Only ever reset and remove a directory THIS process created. A dir the
    # caller pinned belongs to the caller.
    shutil.rmtree(SCRATCH, ignore_errors=True)  # a verdict must never depend
    atexit.register(shutil.rmtree, SCRATCH, True)   # on who ran here before
os.makedirs(STATE_DIR, exist_ok=True)


def _sweep_stale_scratch():
    """Remove scratch dirs whose owning process is gone.

    atexit does NOT run when `timeout` SIGTERMs a run, which is exactly how
    these are usually killed -- so the PID-keyed dirs accumulate. Only a dir
    whose PID no longer exists is touched, so a live sibling run is never
    swept out from under itself.
    """
    import re
    for d in glob.glob(os.path.join(os.path.dirname(SCRATCH), "*-repros-*")):
        m = re.search(r"-repros-(\d+)$", d)
        if not m or d == SCRATCH:
            continue
        try:
            os.kill(int(m.group(1)), 0)      # signal 0: liveness probe only
        except ProcessLookupError:
            shutil.rmtree(d, ignore_errors=True)
        except PermissionError:
            pass                              # someone else's live process
        except Exception:
            pass


_sweep_stale_scratch()
os.environ.setdefault("SPEECH_ENGINE_PATH", os.path.expanduser("~/Projects/speech-to-cli"))


class RecordingInjector:
    """Stands in for the ydotool/IBus backends; records instead of typing."""

    name = "recording"

    def __init__(self):
        self.commits = []       # [(text, monotonic)]
        self.pastes = []
        self.typed = []
        self.preedits = []
        self.backspaces = 0
        self.enters = 0

    # -- lifecycle (no-ops) ------------------------------------------------
    def prepare(self):
        return True

    def acquire(self):
        return True

    def end(self):
        return True

    def cancel(self):
        return True

    def recover(self):
        return True

    def available(self):
        return True

    def supports_preedit(self):
        return False

    # -- text paths --------------------------------------------------------
    def commit(self, text):
        self.commits.append((text, time.monotonic()))
        return True

    def finalize(self, text):
        # The keep-live-text path (#45/#64). A key-event backend already typed
        # it; a pre-edit backend must commit here or end() DISCARDS it. Either
        # way the text stuck, so the fake records it as delivered. Missing this
        # method made any repro reaching step 9's keep-live-text branch die
        # with AttributeError instead of returning a verdict.
        self.commits.append((text, time.monotonic()))
        return True

    def purpose_known(self):
        return False

    def type_text(self, text):
        self.typed.append((text, time.monotonic()))
        return True

    def paste(self, text):
        self.pastes.append((text, time.monotonic()))
        return True

    def set_preedit(self, text):
        self.preedits.append((text, time.monotonic()))
        return True

    def replace_text(self, old_text, new_text):
        self.commits.append((new_text, time.monotonic()))
        return True

    def send_backspaces(self, count):
        self.backspaces += count
        return True

    def press_enter(self):
        self.enters += 1
        return True

    # -- assertions --------------------------------------------------------
    def all_text(self):
        """Every way a transcript can reach the cursor, oldest first."""
        return sorted(self.commits + self.pastes + self.typed, key=lambda p: p[1])



# Every key below is pinned because a repro's verdict depends on it. Nothing
# here may be read from JP's live config -- see the ISOLATION note above.
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
    "chronicle": False,
}



REAL_STATE = os.path.join(os.path.expanduser("~/.local/state"), "gnome-speaks")



@contextlib.contextmanager
def config_scope(mod, **overrides):
    """Run ONE scenario with its own CONFIG flags, restored afterwards.

    Every load() in a process hands back the SAME dict -- `from state import
    CONFIG` binds one shared object across the service, audio, stt, speech_tts
    and wyoming -- so two scenarios in one process silently clobber each
    other's flags, and the second one's verdict describes the first one's
    configuration. Snapshot/restore rather than forking, so the repro stays
    debuggable in one process:

        with harness.config_scope(mod, continuous_dictation=True):
            ...                      # loop-mode scenario
        # flags are back to the pinned baseline here
    

    Three traps a scenario can hit that produce a GREEN run while measuring the
    WRONG code path -- all three found by lucid-pin-lifecycle, and all three
    cost an hour each to rediscover:

    1. `_reload_config_flags()` restores conversation_mode=False at
       start_listening(), so a conversation-mode scenario silently takes the
       DICTATION path (send_backspaces + paste), the reply path never runs, and
       the repro still prints a paste and looks like it passed. The CONFIG_PATH
       redirect in _isolate_config() closes this -- the reload re-reads OUR
       file and re-applies OUR pins -- but only for flags that are IN
       CONFIG_PINS. Put a scenario's flags there or in config_scope(), never in
       a bare CONFIG[...] assignment after load().
    2. In SINGLE-SHOT, turn_end sets _stop_event and
       _stream_conversation_worker aborts on it before stream_chat is ever
       called. Any reply-path scenario needs loop mode, and should assert
       stream_chat was actually invoked so it cannot pass while testing
       something else.
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


def assert_isolated(mod):
    """Prove the module under test cannot reach JP's live state, BEFORE any
    scenario runs.

    CHRONICLE_PATH is computed at MODULE IMPORT from $XDG_STATE_HOME, so this
    only holds if the env var was set before exec_module -- which is easy to
    break by moving an import. The chronicle archive and the rotated
    generations are derived from CHRONICLE_PATH at call time, so pinning it
    pins them too. Raises rather than warns: a repro that has quietly attached
    itself to the real ledger must not be allowed to report a verdict.
    """
    root = os.path.realpath(SCRATCH)
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
                f"{SCRATCH!r} -- refusing to run")
        if os.path.realpath(REAL_STATE) in (real, os.path.dirname(real)):
            raise AssertionError(f"{name} points at JP's live state dir")
    return True


def _isolate_config(mod):
    """Rebuild CONFIG from the service's OWN defaults plus CONFIG_PINS.

    CONFIG is mutated IN PLACE: `from state import CONFIG` means audio, stt,
    speech_tts and wyoming all hold the same dict object, so rebinding
    mod.CONFIG would isolate the service module and miss every other one.
    CONFIG_PATH is repointed at a file holding exactly the same values, so
    _reload_config_flags() re-reading it is a no-op and _save_config_flag()
    can never touch JP's real file.
    """
    real_defaults = mod.state.DEFAULTS_PATH
    try:
        mod.state.DEFAULTS_PATH = os.path.join(SCRATCH, "absent-config.json")
        baseline = mod.state.load_config()
    finally:
        mod.state.DEFAULTS_PATH = real_defaults
    baseline.update(CONFIG_PINS)
    path = os.path.join(SCRATCH, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(baseline, f)
    mod.CONFIG_PATH = path
    mod.CONFIG.clear()
    mod.CONFIG.update(baseline)
    return baseline


def load(fake_tts_seconds=1.0):
    """Import the module under test with the dangerous seams neutralized."""
    sys.path.insert(0, os.path.dirname(SVC_PATH))  # sibling spellbook.py etc.
    spec = importlib.util.spec_from_file_location("gsvc_under_test", SVC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gsvc_under_test"] = mod
    spec.loader.exec_module(mod)  # main() is __main__-guarded; nothing starts

    _isolate_config(mod)   # BEFORE anything reads CONFIG
    assert_isolated(mod)   # and prove it, before any scenario

    mod._schedule_warmup = lambda *a, **k: None
    mod._refresh_audio_detection = lambda *a, **k: None
    mod._prewarm_recorder = lambda *a, **k: None
    mod.clipboard_write = lambda *a, **k: True

    injector = RecordingInjector()
    mod.get_injector = lambda: injector

    events = []          # (kind, tag, monotonic)
    t0 = time.monotonic()

    def fake_tts(text, **kw):
        tag = text.split()[0]
        events.append(("start", tag, time.monotonic() - t0))
        deadline = time.monotonic() + fake_tts_seconds
        while time.monotonic() < deadline:
            if mod.state._cancel_event.is_set():
                events.append(("cancel", tag, time.monotonic() - t0))
                return {"spoken": False, "cancelled": True}
            time.sleep(0.01)
        events.append(("end", tag, time.monotonic() - t0))
        return {"spoken": True}

    mod.speech_tts.tts = fake_tts
    return mod, events, injector


def make_service(mod):
    svc = mod.GnomeSpeaksService()
    svc._audio_detected = True
    svc.play_sound = lambda *a, **k: True
    return svc


def outcome_of(svc, item_id):
    """Terminal outcome recorded for a queue item id, or None."""
    for rec in list(svc._queue_recent):
        if rec.get("id") == item_id:
            return rec.get("outcome")
    return None


def wait_for(pred, timeout=10.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False
