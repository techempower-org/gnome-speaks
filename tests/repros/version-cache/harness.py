"""Shared harness for the /api/version fork-storm repro (issue #53).

Same discipline as the sibling suites: import the real gnome-speaks-service.py
in-process with every dangerous seam stubbed.  No audio, no network, no mic,
no D-Bus, no port 7710, no ~/.config writes, no service restart -- and, the
point of this suite, NO REAL GIT PROCESSES: `subprocess` is swapped for a
counting shim inside the module under test only, so the stdlib module the rest
of the interpreter sees is untouched.

Run any repro with:
    GS_SVC_PATH=<path to a gnome-speaks-service.py> python3 <script>
exit 0 = clean, exit 1 = bug present.
"""
import atexit
import contextlib
import importlib.util
import json
import os
import shutil
import sys

# ENV CONTRACT: GS_SVC_PATH is the ONE input -- the service.py under test.
# The worktree dir is DERIVED from its dirname so sibling modules always come
# from the same tree; GS_WT overrides the dir only to mix trees deliberately.
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

SVC_PATH = os.environ.get(
    "GS_SVC_PATH",
    SVC_DEFAULT)
WT = os.environ.get("GS_WT") or os.path.dirname(os.path.abspath(SVC_PATH))

# Per-PID scratch: several agents run these suites concurrently and a SHARED
# fixed state path fails toward a false regression (fleet notice, 2026-09-06;
# same fix as lucid-cancel-tokens-repros).  One deliberate difference from that
# harness: GS_REPRO_SCRATCH is read with get(), NOT setdefault().  This suite
# SPAWNS a child (dump_payload.py), and an exported scratch path would be
# inherited by it -- whereupon the child's atexit would delete the still-running
# parent's directory.  So the child gets its own, and we only clean up a
# directory we chose ourselves; a caller-pinned path is left alone.
_OWNED = "GS_REPRO_SCRATCH" not in os.environ
SCRATCH = os.environ.get("GS_REPRO_SCRATCH",
                         os.path.join(SCRATCH_ROOT, "version-cache-%d" % os.getpid()))
STATE_DIR = os.path.join(SCRATCH, "state")
os.environ["XDG_STATE_HOME"] = STATE_DIR
os.environ.setdefault("SPEECH_ENGINE_PATH", os.path.expanduser("~/Projects/speech-to-cli"))
if _OWNED:
    # Reset at start as well as exit: a verdict must never depend on who ran
    # here before. Gated on _OWNED for the reason above -- and because rmtree
    # at start on a SHARED path is what made the chronicle suite destroy a peer
    # agent's live directory (see that suite's ISOLATION note).
    shutil.rmtree(SCRATCH, ignore_errors=True)
    atexit.register(shutil.rmtree, SCRATCH, True)   # disposable byproduct, not state
os.makedirs(STATE_DIR, exist_ok=True)


class _CompletedProcess:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class CountingSubprocess:
    """Stands in for the `subprocess` module inside the service's globals.

    Records every argv it is handed and answers git queries from a canned
    repo state, so a repro never depends on the real checkout being clean.
    """

    def __init__(self, hash_="abc1234", branch="perf/version-cache", dirty=False):
        self.calls = []            # [argv]
        self.hash = hash_
        self.branch = branch
        self.dirty = dirty

    # -- the only seam _handle_version uses --------------------------------
    def run(self, argv, *a, **kw):
        self.calls.append(list(argv))
        tail = [x for x in argv if x not in ("git",)]
        text = ""
        if "rev-parse" in tail and "--short" in tail:
            text = self.hash
        elif "rev-parse" in tail and "--abbrev-ref" in tail:
            text = self.branch
        elif "status" in tail:
            text = " M gnome-speaks-service.py\n" if self.dirty else ""
        elif "diff" in tail:
            return _CompletedProcess("", 1 if self.dirty else 0)
        return _CompletedProcess(text + "\n")

    # -- assertions ---------------------------------------------------------
    def git_calls(self):
        return [c for c in self.calls if c and c[0] == "git"]

    def git_count(self):
        return len(self.git_calls())

    def reset(self):
        self.calls = []



CONFIG_PINS = {
    "key": "test-key", "wake_word": False, "wake_word_model": "",
    "wake_word_secure_gate": False, "chronicle": False,
    "continuous_dictation": False, "conversation_mode": False,
    "dictation_mode": True, "terminal_mode": False, "skip_final_paste": False,
    "injection_method": "ydotool", "read_notifications": False,
    "llm_thinking": False, "spiel_provider": False, "debug": False,
    "live_subtitles": False, "phrase_list": [], "wyoming_host": "",
}

REAL_STATE = os.path.join(os.path.expanduser("~/.local/state"), "gnome-speaks")


def _isolate_config(mod):
    """Rebuild CONFIG from the service's OWN defaults plus CONFIG_PINS.

    CONFIG arrived holding JP's LIVE ~/.config/speech-to-cli/config.json (~47
    keys), and _reload_config_flags() re-reads that file MID-RUN for every
    _SYNC_FLAGS key, so pinning a couple of keys after load() was never
    enough. Mutated IN PLACE: `from state import CONFIG` means audio, stt,
    speech_tts and wyoming share the one dict object, so rebinding mod.CONFIG
    would isolate the service module and miss all of them. CONFIG_PATH is
    repointed at a file holding the same values, so the mid-run re-read
    becomes a no-op and _save_config_flag() can never touch the real file.
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


@contextlib.contextmanager
def config_scope(mod, **overrides):
    """Run ONE scenario with its own CONFIG flags, restored afterwards."""
    saved = dict(mod.CONFIG)
    mod.CONFIG.update(overrides)
    try:
        yield mod.CONFIG
    finally:
        mod.CONFIG.clear()
        mod.CONFIG.update(saved)


def assert_isolated(mod):
    """Prove the module cannot reach JP's live state, BEFORE any scenario.

    CHRONICLE_PATH is computed at MODULE IMPORT from $XDG_STATE_HOME, so this
    only holds if the env var was set before exec_module -- one moved import
    away from breaking. Raises rather than warns: a repro that has quietly
    attached itself to the real ledger must not report a verdict.
    """
    root = os.path.realpath(SCRATCH)
    for name in ("CHRONICLE_PATH", "CONFIG_PATH"):
        value = getattr(mod, name, None)
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


def load():
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
    return mod


class VersionClient:
    """Calls _handle_version the way the HTTP server would, minus the socket."""

    def __init__(self, mod, counter=None):
        self.mod = mod
        self.counter = counter or CountingSubprocess()
        mod.subprocess = self.counter          # module-under-test globals only
        self.payloads = []
        # A BaseHTTPRequestHandler that never touches a socket: __init__ is the
        # part that reads the request, so bypass it entirely.
        self.handler = object.__new__(mod.SpeechHTTPHandler)
        self.handler._send_json = self._send_json
        self.handler.service = None

    def _send_json(self, payload, status=200):
        self.payloads.append(payload)

    def get(self, n=1):
        for _ in range(n):
            self.handler._handle_version()
        return self.payloads[-1]


def fresh_cache(mod):
    """Drop any cached payload so a repro measures a cold process."""
    if hasattr(mod.SpeechHTTPHandler, "_version_cache"):
        mod.SpeechHTTPHandler._version_cache = None
