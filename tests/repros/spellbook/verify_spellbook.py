#!/usr/bin/env python3
"""Verify spellbook.py: match(), the denylist floor, overlay merge, op table.

Contract under test (#119):
  1. match(): "cast stop" is the documented panic stop and must CAST, not
     fizzle; a trigger word followed by STT punctuation ("Cast, stop." /
     "Cast. Stop." / "Invoke... skip") or a dictation dash ("Cast - skip")
     must cast, never MISS -- a miss is typed at the cursor verbatim.
     Longest pattern wins ("cast stop the loop now" -> loop, remainder "now").
     No false positive from the normalisation: "cast-iron skillet" and
     "broadcast stop" stay a miss.
  2. _validate(): the DENYLIST is a floor (ydotool, \\block\\., wol sleep
     refuse to load), shell needs a non-empty argv LIST, unknown gate/type,
     missing name/patterns.
  3. load_spellbook(): every repo spell loads; a user overlay overrides by
     name; a BROKEN overlay file never drops the repo spells; a denylisted
     overlay spell is dropped alone; trigger_words override is lowercased.
  4. Op table: every dbus_self.op in spellbook.json has an `op == "<op>"`
     branch in the service's _spell_ctx_dbus -- a typo'd op today is a
     silent FIZZLE_TEXT at runtime, and nothing else checks the join.
  5. USER_SPELLBOOK_PATH seam (#152): the overlay path defaults to the
     ~/.config location, honours GS_SPELLBOOK_USER_PATH verbatim (empty =
     no overlay), and the SERVICE reads the seam rather than a literal --
     that is what lets tests/repros/isolation.py pin it into scratch.

Imports spellbook.py from the tree under test, NOT the service: the service
pulls in speech-to-cli and JP's live config at import, and nothing here needs
either. The op-table check reads the service SOURCE as text. Touches no port,
no D-Bus, no live config; the overlay fixtures live in a per-PID scratch dir.

Baseline: red on 025df92 for the #119 cases (cast stop / Cast, stop. /
Cast - skip / Invoke... skip); everything else green on both sides.
"""
import atexit
import importlib.util
import json
import logging
import os
import re
import shutil
import sys

# ENV CONTRACT (unified 2026-09-06): GS_SVC_PATH is the ONE input -- the
# service.py file under test. The worktree dir is DERIVED from its dirname, so
# spellbook.py and spellbook.json always come from the same tree as the
# service. GS_WT overrides the dir only if you really mean to mix trees.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SVC_DEFAULT = os.path.join(REPO_ROOT, "gnome-speaks-service.py")
SCRATCH_ROOT = os.path.join(REPO_ROOT, "tmp", "repros")

SVC_PATH = os.environ.get("GS_SVC_PATH", SVC_DEFAULT)
WT = os.environ.get("GS_WT") or os.path.dirname(os.path.abspath(SVC_PATH))
MOD_PATH = os.path.join(WT, "spellbook.py")
BOOK_PATH = os.path.join(WT, "spellbook.json")

# Per-PID scratch under the repo's gitignored tmp/ -- never a fixed path, never
# the /tmp tmpfs. Only ever deleted by the process that created it.
_SCRATCH = os.path.join(SCRATCH_ROOT, f"spellbook-{os.getpid()}")
os.makedirs(_SCRATCH, exist_ok=True)
atexit.register(shutil.rmtree, _SCRATCH, True)

FAILS = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILS.append(label)


def load_module():
    spec = importlib.util.spec_from_file_location("gs_spellbook", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gs_spellbook"] = mod
    spec.loader.exec_module(mod)
    return mod


def write_fixture(name, obj):
    path = os.path.join(_SCRATCH, name)
    with open(path, "w") as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f)
    return path


def check_match(sb, book, text, kind, name=None, remainder=None):
    got_kind, got_spell, got_rem = sb.match(text, book)
    got_name = got_spell["name"] if got_spell else None
    ok = got_kind == kind and got_name == name
    if remainder is not None:
        ok = ok and got_rem == remainder
    label = f"match {text!r} -> {kind}"
    if name:
        label += f" {name}"
    if remainder is not None:
        label += f" rem={remainder!r}"
    check(label, ok, f"got ({got_kind}, {got_name}, {got_rem!r})")


def main():
    for p in (MOD_PATH, BOOK_PATH, SVC_PATH):
        if not os.path.isfile(p):
            print(f"!! SETUP FAILURE: no such file {p}")
            return 2
    logging.getLogger("gnome-speaks").setLevel(logging.CRITICAL)
    sb = load_module()
    with open(BOOK_PATH) as f:
        raw = json.load(f)
    repo_names = [s["name"] for s in raw["spells"]]

    print("-- match(): #119 cases (red on 025df92)")
    book = sb.load_spellbook(BOOK_PATH, None)
    check_match(sb, book, "cast stop", "cast", "silence", "")
    check_match(sb, book, "Cast, stop.", "cast", "silence", "")
    check_match(sb, book, "Cast. Stop.", "cast", "silence", "")
    check_match(sb, book, "cast, silence", "cast", "silence", "")
    check_match(sb, book, "Cast - skip", "cast", "skip", "")
    check_match(sb, book, "Invoke... skip", "cast", "skip", "")
    check_match(sb, book, "cast halt", "cast", "silence", "")
    check("silence spell reaches op stop",
          book["spells"]["silence"]["action"].get("op") == "stop")

    print("-- match(): regression guards (green on both sides)")
    check_match(sb, book, "Cast skip!", "cast", "skip", "")
    check_match(sb, book, "invoke echo", "cast", "echo", "")
    check_match(sb, book, "cast the loop", "cast", "loop", "")
    check_match(sb, book, "cast stop the loop", "cast", "loop", "")
    check_match(sb, book, "cast stop the loop now", "cast", "loop", "now")
    check_match(sb, book, "cast", "fizzle", None, "")
    check_match(sb, book, "Cast.", "fizzle", None, "")
    check_match(sb, book, "cast summon dragons", "fizzle", None,
                "summon dragons")
    check_match(sb, book, "hello world", "miss")
    check_match(sb, book, "", "miss")
    check_match(sb, book, "cast-iron skillet", "miss")
    check_match(sb, book, "broadcast stop", "miss")
    check_match(sb, book, "stop", "miss")

    print("-- _validate(): the denylist floor")
    base = {"name": "x", "patterns": ["x"],
            "action": {"type": "say", "text": "hi"}}
    check("valid say spell loads", sb._validate(base) is None)
    # The denylist is matched against the JSON-serialized action, so a
    # phrase pattern (`wol\s+sleep`) sees a shell STRING, not split argv --
    # these fixtures use the string forms the floor is written for.
    for argv, pat in (
            (["ydotool", "key", "1:1"], "ydotool"),
            (["sh", "-c", "realm wol sleep some-host"], "wol sleep"),
            (["curl", "http://x/lock.set"], r"\block\."),
            (["sh", "-c", "loginctl lock-session"], "loginctl lock")):
        err = sb._validate({**base, "action": {"type": "shell",
                                               "argv": argv}})
        check(f"denylist refuses {pat}",
              err is not None and "denylist" in err, repr(err))
    err = sb._validate({**base, "action": {
        "type": "http", "method": "POST",
        "url": "http://ha/api/services/homeassistant/restart"}})
    check("denylist refuses homeassistant/restart",
          err is not None and "denylist" in err, repr(err))
    check("denylist is case-insensitive",
          sb._validate({**base, "action": {"type": "shell",
                                           "argv": ["YDOTOOL"]}})
          is not None)
    check("shell argv string rejected",
          sb._validate({**base, "action": {"type": "shell",
                                           "argv": "echo hi"}})
          == "shell action requires a non-empty argv list")
    check("shell argv empty list rejected",
          sb._validate({**base, "action": {"type": "shell", "argv": []}})
          is not None)
    check("shell argv list accepted",
          sb._validate({**base, "action": {"type": "shell",
                                           "argv": ["echo", "hi"]}}) is None)
    check("unknown action type rejected",
          sb._validate({**base, "action": {"type": "eval"}})
          == "unknown action type 'eval'")
    check("missing action rejected",
          sb._validate({"name": "x", "patterns": ["x"]}) is not None)
    check("unknown gate rejected",
          sb._validate({**base, "gate": "yolo"}) == "unknown gate 'yolo'")
    check("missing name rejected",
          sb._validate({"patterns": ["x"], "action": base["action"]})
          == "missing name")
    check("missing patterns rejected",
          sb._validate({"name": "x", "patterns": [],
                        "action": base["action"]}) == "missing patterns")

    print("-- load_spellbook(): repo + overlay")
    check(f"all {len(repo_names)} repo spells load",
          sorted(book["spells"]) == sorted(repo_names),
          f"loaded {sorted(book['spells'])}")
    check("repo trigger words are cast/invoke",
          book["trigger_words"] == ["cast", "invoke"], book["trigger_words"])
    overlay = write_fixture("overlay.json", {
        "trigger_words": ["Cast", "SUMMON"],
        "spells": [
            {"name": "skip", "patterns": ["onward"],
             "action": {"type": "say", "text": "overridden"}},
            {"name": "evil", "patterns": ["evil"],
             "action": {"type": "shell", "argv": ["ydotool", "type", "x"]}},
            {"name": "greet", "patterns": ["greet"],
             "action": {"type": "say", "text": "hail"}},
        ]})
    merged = sb.load_spellbook(BOOK_PATH, overlay)
    check("overlay overrides by name",
          merged["spells"]["skip"]["patterns"] == ["onward"]
          and merged["spells"]["skip"]["action"]["type"] == "say")
    check("overlay adds a new spell", "greet" in merged["spells"])
    check("denylisted overlay spell dropped alone",
          "evil" not in merged["spells"]
          and len(merged["spells"]) == len(repo_names) + 1,
          sorted(merged["spells"]))
    check("trigger_words override is lowercased",
          merged["trigger_words"] == ["cast", "summon"],
          merged["trigger_words"])
    check_match(sb, merged, "Summon, greet.", "cast", "greet", "")
    check_match(sb, merged, "invoke skip", "miss")
    broken = write_fixture("broken.json", "{not json")
    kept = sb.load_spellbook(BOOK_PATH, broken)
    check("broken overlay file never drops repo spells",
          sorted(kept["spells"]) == sorted(repo_names))
    check("missing overlay path is fine",
          sorted(sb.load_spellbook(
              BOOK_PATH, os.path.join(_SCRATCH, "nope.json"))["spells"])
          == sorted(repo_names))

    print("-- USER_SPELLBOOK_PATH seam (#152; red on a20afea)")
    # The overlay path must be a seam the harnesses can pin, not a literal.
    # Each variant re-execs the module: the env var is read ONCE, at import.
    saved = os.environ.pop("GS_SPELLBOOK_USER_PATH", None)
    try:
        fresh = load_module()
        check("default overlay path is ~/.config/speech-to-cli/spellbook.json",
              getattr(fresh, "USER_SPELLBOOK_PATH", None)
              == os.path.expanduser("~/.config/speech-to-cli/spellbook.json"),
              repr(getattr(fresh, "USER_SPELLBOOK_PATH", None)))
        pinned = os.path.join(_SCRATCH, "pinned-overlay.json")
        os.environ["GS_SPELLBOOK_USER_PATH"] = pinned
        fresh = load_module()
        check("GS_SPELLBOOK_USER_PATH pins the overlay path verbatim",
              getattr(fresh, "USER_SPELLBOOK_PATH", None) == pinned,
              repr(getattr(fresh, "USER_SPELLBOOK_PATH", None)))
        os.environ["GS_SPELLBOOK_USER_PATH"] = ""
        fresh = load_module()
        check("empty GS_SPELLBOOK_USER_PATH means no overlay",
              getattr(fresh, "USER_SPELLBOOK_PATH", None) == ""
              and sorted(fresh.load_spellbook(
                  BOOK_PATH, fresh.USER_SPELLBOOK_PATH)["spells"])
              == sorted(repo_names))
    finally:
        if saved is None:
            os.environ.pop("GS_SPELLBOOK_USER_PATH", None)
        else:
            os.environ["GS_SPELLBOOK_USER_PATH"] = saved
    with open(SVC_PATH) as f:
        src = f.read()
    check("service reads spellbook.USER_SPELLBOOK_PATH",
          "spellbook.USER_SPELLBOOK_PATH" in src)
    check("service has no literal overlay path",
          'expanduser("~/.config/speech-to-cli/spellbook.json")' not in src)

    print("-- op table: spellbook.json <-> _spell_ctx_dbus")
    m = re.search(r"def _spell_ctx_dbus\(self, op\):.*?\n    def ", src,
                  re.S)
    check("service has _spell_ctx_dbus", m is not None)
    body = m.group(0) if m else ""
    ops = sorted({s["action"]["op"] for s in raw["spells"]
                  if s["action"]["type"] == "dbus_self"})
    for op in ops:
        check(f"op {op!r} has a branch", f'op == "{op}"' in body)

    print()
    if FAILS:
        print(f"FAILURES: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("ALL SPELLBOOK CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
