#!/usr/bin/env python3
"""Config-key contract: every key crosses the prefs -> service seam (#120).

`speech-to-cli/state.py::load_config()` WHITELISTS config.json keys -- an
unlisted key is silently dropped, so the prefs row writes it and the Python
side reads its default forever. CLAUDE.md has carried that hazard as prose
since the auto_corrections finding; this is the enforcement. Three set
inclusions, all static (no import of the service, no import of state.py --
importing state reads the live config at module load):

  A  prefs.js keys  ⊆  whitelist ∪ _SYNC_FLAGS ∪ extension.js reads
     Every row prefs writes is read by SOMEONE. extension.js parses config.json
     raw (`_getConfigFlag`), so a key only it reads (`subtitles_user`,
     `show_waveform`) is legitimately absent from the whitelist -- and that set
     is DERIVED from extension.js, not hard-coded, so adding a shell-only key
     needs no edit here and retiring one is noticed.
  B  _SYNC_FLAGS  ⊆  whitelist
     _reload_config_flags() copies these from disk into CONFIG on every
     start_listening(), so an unwhitelisted sync flag is only *mostly* inert:
     it reads its default from service start until the first utterance. Against
     025df92 this names eight keys -- two the service reads (`language`,
     `voice_commands`) and six shell-only `show_*` toggles that never belonged
     in the tuple.
  C  service CONFIG reads  ⊆  prefs.js keys
     Every setting the service consults has a row. A read with no row is a
     setting nobody can change from the UI.

Each extractor has a POSITIVE CONTROL: a floor on how many keys it must find.
A regex that silently matches nothing makes every inclusion vacuously true, so
a floor miss is exit 2 (setup failure), never a pass.

Inputs: GS_SVC_PATH (the service under test; prefs.js and extension.js come
from its directory) and SPEECH_ENGINE_PATH (state.py; defaults to
~/Projects/speech-to-cli like every other suite). Exit 0 clean, 1 an inclusion
fails, 2 a file is missing or an extractor found too little to trust.
"""
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SVC_PATH = os.environ.get("GS_SVC_PATH",
                          os.path.join(REPO_ROOT, "gnome-speaks-service.py"))
WT = os.environ.get("GS_WT") or os.path.dirname(os.path.abspath(SVC_PATH))
ENGINE = os.environ.get("SPEECH_ENGINE_PATH",
                        os.path.expanduser("~/Projects/speech-to-cli"))
STATE_PATH = os.path.join(ENGINE, "state.py")
PREFS_PATH = os.path.join(WT, "prefs.js")
EXT_PATH = os.path.join(WT, "extension.js")

KEY = r"""["']([a-z][a-z0-9_]*)["']"""

# prefs.js row helpers -> which positional argument is the config key. The
# GSettings and cloud-chat-assistant (_addCca*) helpers write other stores and
# are deliberately not listed.
PREFS_HELPERS = {
    "_addSwitchRow": 3,      # (group, title, subtitle, configKey, ...)
    "_addComboRow": 2,       # (group, title, configKey, ...)
    "_addRegionCombo": 2,
    "_addSpinRow": 2,
    "_addEntryRow": 2,
    "_addPasswordRow": 2,
    "_addListEntryRow": 2,
    "_setConfigValue": 0,    # (key, value) -- the bespoke rows
    "_deleteConfigKey": 0,
}

# Positive-control floors: measured on 025df92 (prefs 77, whitelist 67,
# sync 47, service 39, extension 11) and set well under those so ordinary
# churn does not trip them while a broken regex still does.
FLOORS = {"prefs": 40, "whitelist": 30, "sync": 15, "service": 20, "ext": 4}

FAILS = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILS.append(label)


def setup_fail(msg):
    print(f"!! SETUP FAILURE: {msg}")
    sys.exit(2)


def read(path):
    if not os.path.isfile(path):
        setup_fail(f"missing {path}")
    with open(path) as f:
        return f.read()


def strip_py_comments(src):
    # Good enough for the blocks scanned here: no '#' inside a string literal.
    return re.sub(r"#.*", "", src)


def split_args(src, start):
    """Split the argument list of a call whose '(' is at src[start].

    Depth-aware over ()[]{} and JS string literals (incl. template strings),
    so a translated title containing a comma does not shift the positions.
    Returns (args, end_index) or (None, start) if the call never closes.
    """
    assert src[start] == "("
    depth, i, n = 0, start, len(src)
    args, cur, quote = [], [], None
    while i < n:
        ch = src[i]
        if quote:
            cur.append(ch)
            if ch == "\\":
                i += 1
                if i < n:
                    cur.append(src[i])
            elif ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            if depth > 1:
                cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                args.append("".join(cur).strip())
                return args, i
            cur.append(ch)
        elif ch == "," and depth == 1:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    return None, start


def literal(arg):
    m = re.fullmatch(KEY, arg or "")
    return m.group(1) if m else None


def prefs_keys(src):
    keys = set()
    for name, idx in PREFS_HELPERS.items():
        for m in re.finditer(r"this\.%s\(" % re.escape(name), src):
            args, _ = split_args(src, m.end() - 1)
            if args is None or len(args) <= idx:
                continue
            k = literal(args[idx])
            # A non-literal is a helper forwarding its own `configKey`
            # parameter (the definitions of _addSwitchRow etc. call
            # _setConfigValue(configKey, …)); the literal is at the call site.
            if k:
                keys.add(k)
    return keys


def whitelist_keys(src):
    m = re.search(r"^def load_config\(\):\n(.*?)^(?=def |[A-Z_]+ = )", src,
                  re.S | re.M)
    if not m:
        setup_fail("could not find load_config() in state.py")
    body = strip_py_comments(m.group(1))
    return set(re.findall(r'^\s*"([a-z][a-z0-9_]*)":', body, re.M))


def sync_flags(src):
    m = re.search(r"^    _SYNC_FLAGS = \((.*?)^    \)", src, re.S | re.M)
    if not m:
        setup_fail("could not find _SYNC_FLAGS in the service")
    return set(re.findall(KEY, strip_py_comments(m.group(1))))


def service_reads(src):
    keys = set()
    for pat in (r"(?<![\w.])CONFIG\.get\(\s*" + KEY,
                r"(?<![\w.])CONFIG\[" + KEY + r"\]",
                r"_save_config_flag\(\s*" + KEY):
        keys.update(re.findall(pat, src))
    return keys


def extension_reads(src):
    return set(re.findall(r"_getConfigFlag\(\s*" + KEY, src))


def floor(name, keys):
    if len(keys) < FLOORS[name]:
        setup_fail(f"{name}: extracted only {len(keys)} keys "
                   f"(floor {FLOORS[name]}) -- the extractor is broken, "
                   f"not the tree: {sorted(keys)}")
    print(f"  {name:<9} {len(keys):>3} keys")


def main():
    svc = read(SVC_PATH)
    prefs = prefs_keys(read(PREFS_PATH))
    wl = whitelist_keys(read(STATE_PATH))
    sync = sync_flags(svc)
    reads = service_reads(svc)
    ext = extension_reads(read(EXT_PATH))
    for name, keys in (("prefs", prefs), ("whitelist", wl), ("sync", sync),
                       ("service", reads), ("ext", ext)):
        floor(name, keys)

    a = sorted(prefs - wl - sync - ext)
    check("A prefs keys ⊆ whitelist ∪ _SYNC_FLAGS ∪ extension reads",
          not a, f"-- written by prefs, read by nobody: {a}")
    b = sorted(sync - wl)
    check("B _SYNC_FLAGS ⊆ whitelist",
          not b, f"-- synced but dropped by load_config(): {b}")
    c = sorted(reads - prefs)
    check("C service CONFIG reads ⊆ prefs keys",
          not c, f"-- read by the service, no prefs row: {c}")

    if FAILS:
        print(f"FAILURES: {len(FAILS)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
