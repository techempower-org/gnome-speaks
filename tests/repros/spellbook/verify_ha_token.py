#!/usr/bin/env python3
"""Verify _get_ha_token() reads its sources from CONFIG, not from code (#129).

Before #129 the service baked one user's setup into the public service:
`~/.cache/ha-token-tmp` as a cache path and `bw get password <personal item>`
as the vault lookup -- a 10 s `bw` timeout on every "assist" cast for anyone
without that item (i.e. every extensions.gnome.org user). Contract:

  H1  documented default: with HA_TOKEN unset and neither key configured, the
      token is None AND `bw` is NEVER invoked (the fake bw records its argv)
  H2  HA_TOKEN in the environment wins, still without a bw call
  H3  ha_token_cache names the file; its content (stripped) is the token
  H4  a missing/empty cache file falls through, silently
  H5  ha_token_item names the vault item: `bw get password <that item>`, and
      bw's stdout (stripped) is the token
  H6  bw failing (rc != 0) -> None, no exception
  H7  both keys are in _SYNC_FLAGS, so a prefs edit applies without a restart
  H8  no personal literal survives in the function: neither the old path nor
      the old item name appears in _get_ha_token's source

The fake `bw` is a per-run script placed FIRST on PATH; it appends its argv to
a log file in the per-PID scratch dir, so "bw was not called" is a measured
zero with a positive control (H5 proves the same fake IS reached when asked).

Baseline (a20afea, pre-#129): H1 FAILS (bw invoked with the personal item),
H5 FAILS (item name ignored), H7/H8 FAIL; H2/H4/H6 pass on both sides.
Exit 0 = all hold; 1 = a verdict failed; 2 = setup failure.
"""
import atexit
import importlib.util
import inspect
import os
import shutil
import stat
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(REPO_ROOT, "tests", "repros"))
import isolation  # noqa: E402

SVC_PATH = os.environ.get("GS_SVC_PATH",
                          os.path.join(REPO_ROOT, "gnome-speaks-service.py"))
SCRATCH_ROOT = os.path.join(REPO_ROOT, "tmp", "repros")
SCRATCH, _STATE = isolation.setup_scratch("spellbook-hatoken-repros", SCRATCH_ROOT)
isolation.sweep_stale_scratch(SCRATCH)

BW_LOG = os.path.join(SCRATCH, "bw-calls.log")
BW_DIR = os.path.join(SCRATCH, "bin")
FAILS = []


def check(label, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} ({label}) {detail}")
    if not cond:
        FAILS.append(label)


def redact(tok, expected=None):
    """Describe a token WITHOUT printing it. Against the pre-#129 service on
    the maintainer's machine the default path returns a REAL long-lived token
    (the personal cache file exists there), so a detail line that echoed the
    value would leak it into every log this suite writes."""
    if tok is None:
        return "None"
    if expected is not None:
        return "<expected>" if tok == expected else f"<UNEXPECTED, {len(tok)} chars>"
    return f"<{len(tok)} chars>"


def install_fake_bw(rc=0, out="vault-token-value\n"):
    os.makedirs(BW_DIR, exist_ok=True)
    path = os.path.join(BW_DIR, "bw")
    with open(path, "w") as f:
        f.write("#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{BW_LOG}'\n"
                f"printf '%s' '{out}'\n"
                f"exit {rc}\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    os.environ["PATH"] = BW_DIR + os.pathsep + os.environ.get("PATH", "")


def bw_calls():
    try:
        with open(BW_LOG) as f:
            return [l.rstrip("\n") for l in f if l.strip()]
    except OSError:
        return []


def reset_bw_log():
    try:
        os.remove(BW_LOG)
    except OSError:
        pass


def load():
    sys.path.insert(0, os.path.dirname(SVC_PATH))
    spec = importlib.util.spec_from_file_location("gsvc_hatoken", SVC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gsvc_hatoken"] = mod
    spec.loader.exec_module(mod)
    isolation.isolate_config(mod, SCRATCH)
    isolation.assert_isolated(mod, SCRATCH)
    return mod


def main():
    if not os.path.isfile(SVC_PATH):
        print(f"!! SETUP FAILURE: no such file: {SVC_PATH}")
        return 2
    os.environ.pop("HA_TOKEN", None)
    install_fake_bw()
    # Positive control for the fake: it must be the bw that PATH resolves.
    if shutil.which("bw") != os.path.join(BW_DIR, "bw"):
        print(f"!! SETUP FAILURE: PATH does not resolve to the fake bw: {shutil.which('bw')}")
        return 2
    mod = load()
    fn = getattr(mod, "_get_ha_token", None)
    if fn is None:
        print("!! SETUP FAILURE: service has no _get_ha_token")
        return 2
    # The isolated CONFIG is rebuilt from load_config() defaults. Against a
    # speech-to-cli that has not whitelisted the keys yet, they are simply
    # absent -- which _get_ha_token must treat exactly like "".
    mod.CONFIG.pop("ha_token_item", None)
    mod.CONFIG.pop("ha_token_cache", None)

    # H1 -- the documented default: nothing configured -> None, and bw untouched
    reset_bw_log()
    tok = fn()
    calls = bw_calls()
    check("H1", tok is None and calls == [],
          f"default: token={redact(tok)} bw_calls={calls}")

    # H2 -- environment wins
    reset_bw_log()
    os.environ["HA_TOKEN"] = "  env-token  "
    try:
        tok = fn()
    finally:
        os.environ.pop("HA_TOKEN", None)
    check("H2", tok == "env-token" and bw_calls() == [],
          f"env: token={redact(tok, 'env-token')} bw_calls={bw_calls()}")

    # H3 -- cache file from CONFIG
    reset_bw_log()
    cache = os.path.join(SCRATCH, "ha-token-cache")
    with open(cache, "w") as f:
        f.write("file-token-value\n")
    with isolation.config_scope(mod, ha_token_cache=cache):
        tok = fn()
    check("H3", tok == "file-token-value" and bw_calls() == [],
          f"cache: token={redact(tok, 'file-token-value')} bw_calls={bw_calls()}")

    # H4 -- missing and empty cache files fall through (to nothing, here)
    reset_bw_log()
    empty = os.path.join(SCRATCH, "empty-cache")
    open(empty, "w").close()
    with isolation.config_scope(mod, ha_token_cache=os.path.join(SCRATCH, "no-such-file")):
        t1 = fn()
    with isolation.config_scope(mod, ha_token_cache=empty):
        t2 = fn()
    check("H4", t1 is None and t2 is None and bw_calls() == [],
          f"missing={redact(t1)} empty={redact(t2)} bw_calls={bw_calls()}")

    # H5 -- vault item from CONFIG, and the fake bw is actually reached
    reset_bw_log()
    with isolation.config_scope(mod, ha_token_item="my-own-item-name"):
        tok = fn()
    calls = bw_calls()
    check("H5", tok == "vault-token-value" and calls == ["get password my-own-item-name"],
          f"item: token={redact(tok, 'vault-token-value')} bw_calls={calls}")

    # H6 -- bw failure is a None, not an exception
    install_fake_bw(rc=1, out="")
    reset_bw_log()
    try:
        with isolation.config_scope(mod, ha_token_item="my-own-item-name"):
            tok = fn()
        ok = tok is None and bw_calls() == ["get password my-own-item-name"]
        detail = f"bw rc=1: token={redact(tok)} bw_calls={bw_calls()}"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"raised {exc!r}"
    check("H6", ok, detail)

    # H7 -- live-applied: both keys are sync flags
    sync = ()
    for obj in vars(mod).values():   # the service class, found by attribute
        if isinstance(obj, type) and hasattr(obj, "_SYNC_FLAGS"):
            sync = obj._SYNC_FLAGS
            break
    check("H7", "ha_token_item" in sync and "ha_token_cache" in sync,
          f"_SYNC_FLAGS has item={'ha_token_item' in sync} cache={'ha_token_cache' in sync}")

    # H8 -- no personal literal in the function. Assembled, so this file is
    # not itself a hit for a grep of the tree.
    src = inspect.getsource(fn)
    old_path = "ha-token" + "-tmp"
    old_item = "ha-" + "llat"
    check("H8", old_path not in src and old_item not in src,
          f"old path present={old_path in src} old item present={old_item in src}")

    if FAILS:
        print(f"FAIL: {len(FAILS)} verdict(s) -- {FAILS}")
        return 1
    print("RESULT: HA TOKEN SOURCES ARE CONFIG")
    return 0


if __name__ == "__main__":
    sys.exit(main())
