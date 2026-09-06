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
import importlib.util
import json
import os
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

# The shared isolation core (#90). Five harnesses carried a byte-identical copy
# of it; a correction to the contract had to be made five times.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import isolation  # noqa: E402


SVC_PATH = os.environ.get("GS_SVC_PATH", SVC_DEFAULT)
WT = os.environ.get("GS_WT") or os.path.dirname(os.path.abspath(SVC_PATH))

# get(), never setdefault(): setdefault EXPORTS the path, so any child python
# this suite spawns would inherit the PARENT's dir and its atexit would delete
# the still-running parent's scratch out from under it.
SCRATCH, STATE_DIR = isolation.setup_scratch("cancel-repros", SCRATCH_ROOT)
isolation.sweep_stale_scratch(SCRATCH)


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
CONFIG_PINS = dict(isolation.CONFIG_PINS)

# Re-exported so callers keep working: subtitle_spy delegates its PATH checks
# to harness.assert_isolated() and adds the "pins actually took" half, and
# repros call harness.config_scope() directly.
config_scope = isolation.config_scope


def assert_isolated(mod):
    return isolation.assert_isolated(mod, SCRATCH)


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

    isolation.isolate_config(mod, SCRATCH, CONFIG_PINS)   # BEFORE anything reads CONFIG
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
