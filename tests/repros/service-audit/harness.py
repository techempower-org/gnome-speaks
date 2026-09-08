"""Shared harness: import gnome-speaks-service.py with every dangerous seam stubbed.

No audio, no network, no mic, no D-Bus, no port 7710, no writes outside /tmp.
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
SCRATCH, STATE_DIR = isolation.setup_scratch("audit-repros", SCRATCH_ROOT)
isolation.sweep_stale_scratch(SCRATCH)



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
    sys.path.insert(0, os.path.dirname(SVC_PATH))  # sibling spellbook.py
    spec = importlib.util.spec_from_file_location("gsvc_under_test", SVC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gsvc_under_test"] = mod
    spec.loader.exec_module(mod)  # main() is __main__-guarded; nothing starts

    isolation.isolate_config(mod, SCRATCH, CONFIG_PINS)   # BEFORE anything reads CONFIG
    assert_isolated(mod)   # and prove it, before any scenario

    # --- neutralize every side effect before any instance exists ---
    mod._schedule_warmup = lambda *a, **k: None
    mod._refresh_audio_detection = lambda *a, **k: None
    mod._prewarm_recorder = lambda *a, **k: None
    mod.type_at_cursor = lambda *a, **k: None
    mod.clipboard_write = lambda *a, **k: True

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

    isolation.install_fake_tts(mod, fake_tts)   # also nulls the #134 seam
    return mod, events


def make_service(mod):
    svc = mod.GnomeSpeaksService()
    # #152: the overlay pin is a path; prove it TOOK by counting what this
    # instance actually loaded against the repo spellbook.json.
    isolation.assert_repo_spellbook(svc, SVC_PATH)
    svc._audio_detected = True
    svc.play_sound = lambda *a, **k: True
    return svc


def spans(events):
    """Collapse the event log into {tag: (start, end)} playback spans."""
    out = {}
    for kind, tag, ts in events:
        if kind == "start":
            out.setdefault(tag, [ts, None])
        else:
            if tag in out:
                out[tag][1] = ts
    return {k: tuple(v) for k, v in out.items()}


def overlap(a, b):
    (a0, a1), (b0, b1) = a, b
    a1 = a1 if a1 is not None else 1e9
    b1 = b1 if b1 is not None else 1e9
    return max(0.0, min(a1, b1) - max(a0, b0))
