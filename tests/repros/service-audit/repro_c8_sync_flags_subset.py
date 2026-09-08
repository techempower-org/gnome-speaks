#!/usr/bin/env python3
"""repro: _SYNC_FLAGS is a second config whitelist, and it must agree with the
first (#127).

_reload_config_flags() copies every _SYNC_FLAGS key from config.json into
CONFIG -- but only from a non-quick start_listening(). speech-to-cli's
state.load_config() is the whitelist that fills CONFIG at import. A key in
_SYNC_FLAGS but not in load_config() therefore reads its default until the
first hotkey press, and a wake-word-first session (quick=True) never gets it
at all. That is how `phrase_list` (#106) and then `language` / `voice_commands`
went missing, and nothing asserted the relation.

  S1  every _SYNC_FLAGS key is a load_config() key   (subset of the real whitelist)
  S2  every _SYNC_FLAGS key has a Python reader      (not an extension.js-only key)

S2 scans the service, its sibling modules and speech-to-cli for the quoted key
outside the _SYNC_FLAGS tuple itself. It found six show_* keys synced for
nobody: only extension.js/prefs.js ever read them, and GJS parses config.json
raw, so syncing them into CONFIG did nothing.

Needs speech-to-cli (SPEECH_ENGINE_PATH). Before the matching speech-to-cli
fix, S1 is red on `language` and `voice_commands` -- that is the verdict, not
a setup failure.
Run:  GS_SVC_PATH=<gnome-speaks-service.py> python3 repro_c8_sync_flags_subset.py
Exit 0 = both hold; 1 = a verdict failed; 2 = setup failure.
"""
import ast
import atexit
import glob
import importlib.util
import os
import re
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SVC_PATH = os.environ.get("GS_SVC_PATH", os.path.join(REPO_ROOT, "gnome-speaks-service.py"))
SCRATCH_ROOT = os.path.join(REPO_ROOT, "tmp", "repros")
_STATE = os.path.join(SCRATCH_ROOT, f"service-audit-syncflags-{os.getpid()}", "state")
os.makedirs(_STATE, exist_ok=True)
atexit.register(shutil.rmtree, os.path.dirname(_STATE), True)
os.environ["XDG_STATE_HOME"] = _STATE
os.environ.setdefault("SPEECH_ENGINE_PATH", os.path.expanduser("~/Projects/speech-to-cli"))
FAILS = []


def check(label, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} ({label}) {detail}")
    if not cond:
        FAILS.append(label)


def load():
    sys.path.insert(0, os.path.dirname(SVC_PATH))
    spec = importlib.util.spec_from_file_location("gsvc_syncflags", SVC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gsvc_syncflags"] = mod
    spec.loader.exec_module(mod)
    mod.CONFIG_PATH = os.path.join(_STATE, "config.json")
    return mod


def source_without_sync_flags(path):
    """The service source with the _SYNC_FLAGS tuple blanked out, so the tuple
    cannot count as its own reader."""
    src = open(path).read()
    lines = src.splitlines()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_SYNC_FLAGS" for t in node.targets):
            for i in range(node.lineno - 1, node.end_lineno):
                lines[i] = ""
    return "\n".join(lines)


def python_readers(key, sources):
    pat = re.compile(r"""['"]%s['"]""" % re.escape(key))
    return sorted(name for name, text in sources if pat.search(text))


def main():
    if not os.path.isfile(SVC_PATH):
        print(f"!! SETUP FAILURE: no such file: {SVC_PATH}")
        return 2
    engine = os.environ["SPEECH_ENGINE_PATH"]
    if not os.path.isfile(os.path.join(engine, "state.py")):
        print(f"!! SETUP FAILURE: no speech-to-cli at SPEECH_ENGINE_PATH={engine}")
        return 2
    mod = load()
    flags = getattr(getattr(mod, "GnomeSpeaksService", None), "_SYNC_FLAGS", None)
    if not flags:
        print("!! SETUP FAILURE: GnomeSpeaksService._SYNC_FLAGS not found")
        return 2

    # load_config() with NO config file: the whitelist's own keys, nothing from
    # JP's live config leaking in as if it were whitelisted.
    st = mod.state
    real_defaults = st.DEFAULTS_PATH
    st.DEFAULTS_PATH = os.path.join(_STATE, "absent-config.json")
    try:
        whitelist = set(st.load_config())
    finally:
        st.DEFAULTS_PATH = real_defaults

    missing = sorted(k for k in flags if k not in whitelist)
    check("S1", not missing,
          f"{len(flags)} _SYNC_FLAGS, {len(whitelist)} load_config keys; "
          f"synced but NOT whitelisted: {missing or 'none'}")

    svc_dir = os.path.dirname(os.path.abspath(SVC_PATH))
    sources = [(os.path.basename(SVC_PATH), source_without_sync_flags(SVC_PATH))]
    for d in (svc_dir, engine):
        for f in sorted(glob.glob(os.path.join(d, "*.py"))):
            if os.path.abspath(f) == os.path.abspath(SVC_PATH):
                continue
            sources.append((os.path.relpath(f, REPO_ROOT), open(f).read()))
    dead = sorted(k for k in flags if not python_readers(k, sources))
    check("S2", not dead,
          f"synced with NO Python reader (extension.js-only?): {dead or 'none'}")

    if FAILS:
        print(f"FAIL: {len(FAILS)} verdict(s) -- _SYNC_FLAGS disagrees with load_config()")
        return 1
    print(f"PASS: all {len(flags)} _SYNC_FLAGS keys are whitelisted in load_config() and read by Python")
    return 0


if __name__ == "__main__":
    sys.exit(main())
