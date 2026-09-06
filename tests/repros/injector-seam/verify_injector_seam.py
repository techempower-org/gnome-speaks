#!/usr/bin/env python3
"""Verify the Phase-1 Injector seam in gnome-speaks-service.py.

Contract under test:
  1. get_injector() yields a YdotoolInjector by default, and is a singleton.
  2. Every seam method delegates to the original module-level function,
     with the arguments and return value passed through untouched.
  3. injection_method: ibus / auto / garbage all fall back to ydotool;
     ibus and garbage log a warning.
  4. available() tracks _TYPING_TOOL; supports_preedit() is False.
  5. No call site outside the seam names a ydotool function (static scan).

Touches nothing real: every ydotool/clipboard function is replaced by a spy
before it can be called.  No port 7710, no service restart, no uinput.
"""
import atexit
import importlib.util
import io
import logging
import os
import re
import shutil
import sys

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

# disk-backed scratch, never the /tmp tmpfs
_STATE = os.path.join(SCRATCH_ROOT, f"morpheus-injector-seam-{os.getpid()}", "state")
os.makedirs(_STATE, exist_ok=True)
atexit.register(shutil.rmtree, os.path.dirname(_STATE), True)
os.environ["XDG_STATE_HOME"] = _STATE
os.environ.setdefault("SPEECH_ENGINE_PATH", os.path.expanduser("~/Projects/speech-to-cli"))

FAILS = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILS.append(label)


def load():
    sys.path.insert(0, os.path.dirname(SVC_PATH))  # sibling spellbook.py
    spec = importlib.util.spec_from_file_location("gsvc_seam", SVC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gsvc_seam"] = mod
    spec.loader.exec_module(mod)  # main() is __main__-guarded; nothing starts
    # CONFIG arrives holding JP's LIVE ~/.config/speech-to-cli/config.json
    # (~47 keys; terminal_mode and wake_word are True on this machine). Pin
    # every key this file's verdicts can see, in place -- `from state import
    # CONFIG` means audio/stt/speech_tts share the same dict object. Point
    # CONFIG_PATH somewhere we own too: _reload_config_flags() re-reads it
    # mid-run and _save_config_flag() writes it.
    mod.CONFIG.update({
        "key": "test-key", "wake_word": False, "wake_word_model": "",
        "wake_word_secure_gate": False, "chronicle": False,
        "continuous_dictation": False, "conversation_mode": False,
        "dictation_mode": True, "terminal_mode": False,
        "skip_final_paste": False, "read_notifications": False,
        "spiel_provider": False, "debug": False, "wyoming_host": "",
    })
    mod.CONFIG.pop("injection_method", None)   # each test sets it explicitly
    os.makedirs(_STATE, exist_ok=True)
    mod.CONFIG_PATH = os.path.join(_STATE, "config.json")
    return mod


def fresh(mod, method=None):
    """Rebuild the singleton, optionally under a given injection_method."""
    if method is None:
        mod.CONFIG.pop("injection_method", None)
    else:
        mod.CONFIG["injection_method"] = method
    mod._injector = None
    return mod.get_injector()


class Spy:
    def __init__(self, ret=None):
        self.calls = []
        self.ret = ret

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        return self.ret


def _raises(obj, meth, *args):
    try:
        getattr(obj, meth)(*args)
        return False
    except NotImplementedError:
        return True


def capture_log(mod):
    """Attach a buffer to the service logger; returns (buffer, detach)."""
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setLevel(logging.DEBUG)
    mod.log.addHandler(h)
    return buf, lambda: mod.log.removeHandler(h)


def main():
    print(f"service under test: {SVC_PATH}")
    mod = load()

    # ---- 1. default selection + singleton -------------------------------
    print("\n[1] default backend selection")
    inj = fresh(mod)
    check("default is YdotoolInjector", type(inj) is mod.YdotoolInjector, type(inj))
    check("YdotoolInjector is an Injector", isinstance(inj, mod.Injector))
    check("name == 'ydotool'", inj.name == "ydotool", inj.name)
    check("get_injector() is a singleton", mod.get_injector() is inj)
    check("explicit 'ydotool' selects it too",
          type(fresh(mod, "ydotool")) is mod.YdotoolInjector)

    # ---- 2. delegation --------------------------------------------------
    print("\n[2] seam methods delegate to the original implementations")
    inj = fresh(mod)
    cases = [
        # (module function, seam method, args, spy return, expected return)
        ("type_at_cursor",     "commit",          ("hello",),      True,  True),
        ("_type_raw",          "type_text",       (" ",),          None,  None),
        ("_send_backspaces",   "send_backspaces", (7,),            None,  None),
        ("replace_typed_text", "replace_text",    ("old", "new"),  None,  None),
        ("_clipboard_paste",   "paste",           ("pasted",),     True,  True),
        ("_reset_ydotoold",    "recover",         (),              None,  None),
    ]
    for fn_name, meth_name, args, spy_ret, want in cases:
        original = getattr(mod, fn_name)
        spy = Spy(spy_ret)
        setattr(mod, fn_name, spy)
        try:
            got = getattr(inj, meth_name)(*args)
        finally:
            setattr(mod, fn_name, original)
        check(f"{meth_name}() -> {fn_name}(): called once",
              len(spy.calls) == 1, spy.calls)
        check(f"{meth_name}() -> {fn_name}(): args passed through",
              spy.calls and spy.calls[0] == (args, {}), spy.calls)
        check(f"{meth_name}() -> {fn_name}(): return passed through",
              got == want, repr(got))
        check(f"{fn_name} restored after patch",
              getattr(mod, fn_name) is original)

    # prepare() == detect + reset, in that order
    order = []
    o_detect, o_reset = mod._detect_typing_tool, mod._reset_ydotoold
    mod._detect_typing_tool = lambda: order.append("detect")
    mod._reset_ydotoold = lambda: order.append("reset")
    try:
        inj.prepare()
    finally:
        mod._detect_typing_tool, mod._reset_ydotoold = o_detect, o_reset
    check("prepare() -> _detect_typing_tool() then _reset_ydotoold()",
          order == ["detect", "reset"], order)

    # ---- 3. backend selection -------------------------------------------
    # NOTE: a real IbusInjector.available() registers a component with the
    # live ibus daemon. Tests must never do that, so IbusInjector is stubbed
    # for every case below -- selection logic is what is under test here, and
    # the backend itself has its own mock-bus suite.
    print("\n[3] backend selection and fallback")
    real_ibus_cls = mod.IbusInjector

    class StubIbus(mod.Injector):
        name = "ibus"
        reachable = True

        def __init__(self, fallback=None):
            self.fallback = fallback

        def available(self):
            return StubIbus.reachable

    try:
        # (a) no IBus backend importable at all -> everything falls to ydotool
        mod.IbusInjector = None
        for method, want_warn in [("ibus", True), ("auto", False), ("uinput", True),
                                  ("YDOTOOL", False), ("", False), (None, False)]:
            buf, detach = capture_log(mod)
            try:
                got = fresh(mod, method)
            finally:
                detach()
            text = buf.getvalue()
            label = repr(method)
            check(f"{label} falls back to YdotoolInjector",
                  type(got) is mod.YdotoolInjector, type(got))
            if want_warn:
                check(f"{label} logs a warning",
                      "could not be imported" in text or "Unknown injection_method" in text,
                      repr(text))
            else:
                check(f"{label} logs no warning",
                      "Unknown injection_method" not in text
                      and "could not be imported" not in text, repr(text))
        check("'  YdoTool ' is case/space normalized",
              type(fresh(mod, "  YdoTool ")) is mod.YdotoolInjector)

        # (b) IBus present and reachable -> ibus/auto select it, ydotool does not
        mod.IbusInjector = StubIbus
        StubIbus.reachable = True
        check("'ibus' selects the IBus backend when reachable",
              type(fresh(mod, "ibus")) is StubIbus, type(fresh(mod, "ibus")))
        check("'auto' prefers IBus when reachable",
              type(fresh(mod, "auto")) is StubIbus)
        check("'ydotool' still means ydotool even when IBus is reachable",
              type(fresh(mod, "ydotool")) is mod.YdotoolInjector)
        check("default (unset) is ydotool even when IBus is reachable",
              type(fresh(mod)) is mod.YdotoolInjector)
        sel = fresh(mod, "ibus")
        check("IBus backend is given a ydotool fallback for keystrokes",
              type(sel.fallback) is mod.YdotoolInjector, sel.fallback)

        # (c) IBus present but unreachable -> both fall back, never to nothing
        StubIbus.reachable = False
        for method in ("ibus", "auto"):
            buf, detach = capture_log(mod)
            try:
                got = fresh(mod, method)
            finally:
                detach()
            check(f"{method!r} falls back when IBus is unreachable",
                  type(got) is mod.YdotoolInjector, type(got))
            check(f"{method!r} says why", "unavailable" in buf.getvalue(),
                  repr(buf.getvalue()))
    finally:
        mod.IbusInjector = real_ibus_cls
        mod._injector = None

    # ---- 3b. the escape hatch: config edit alone switches back -----------
    print("\n[3b] config-only escape hatch")
    mod.IbusInjector = StubIbus
    StubIbus.reachable = True
    try:
        first = fresh(mod, "ibus")
        check("started on the IBus backend", type(first) is StubIbus)
        mod.CONFIG["injection_method"] = "ydotool"   # a bare config edit
        second = mod.get_injector()                  # no restart, no reset
        check("editing injection_method alone switches back to ydotool",
              type(second) is mod.YdotoolInjector, type(second))
        check("...without needing the singleton to be cleared by hand",
              second is not first)
    finally:
        mod.IbusInjector = real_ibus_cls
        mod._injector = None
        mod.CONFIG.pop("injection_method", None)

    # ---- 4. capability probes -------------------------------------------
    print("\n[4] capability probes")
    inj = fresh(mod)
    check("supports_preedit() is False (phase 1)", inj.supports_preedit() is False)
    saved = mod._TYPING_TOOL
    try:
        for tool, want in [("ydotool", True), ("xdotool", True),
                           ("clipboard", False), (None, False)]:
            mod._TYPING_TOOL = tool
            check(f"available() is {want} for _TYPING_TOOL={tool!r}",
                  inj.available() is want, inj.available())
    finally:
        mod._TYPING_TOOL = saved
    base = mod.Injector()
    check("base Injector.available() is False", base.available() is False)
    check("base Injector.supports_preedit() is False", base.supports_preedit() is False)
    for meth, args in [("commit", ("x",)), ("type_text", ("x",)),
                       ("send_backspaces", (1,)), ("replace_text", ("a", "b")),
                       ("paste", ("x",))]:
        try:
            getattr(base, meth)(*args)
            check(f"base Injector.{meth}() raises NotImplementedError", False, "no raise")
        except NotImplementedError:
            check(f"base Injector.{meth}() raises NotImplementedError", True)

    # ---- 4b. the two-category rule ---------------------------------------
    # TEXT (commit/type_text/...) and KEYS (press_enter) must never be merged:
    # commit_text("\n") inserts a newline character, so a shell never runs the
    # command. This check exists to fail loudly if a future sweep folds them.
    print("\n[4b] text path and key path stay distinct")
    y = mod.YdotoolInjector()
    check("press_enter is its own method, not an alias of type_text",
          mod.Injector.press_enter is not mod.Injector.type_text)
    check("YdotoolInjector keeps them distinct",
          type(y).press_enter is not type(y).type_text)
    check("base Injector.press_enter() raises NotImplementedError",
          _raises(mod.Injector(), "press_enter"))
    check("the seam exposes no generic 'type_raw' any more",
          not hasattr(mod.Injector, "type_raw"))

    # ---- 5. static scan: nothing bypasses the seam -----------------------
    print("\n[5] static scan — no injection call outside the seam")
    src = open(SVC_PATH, encoding="utf-8").read().split("\n")
    guarded = re.compile(
        r"\b(type_at_cursor|_type_raw|_send_backspaces|replace_typed_text"
        r"|_clipboard_paste|_reset_ydotoold|_detect_typing_tool|_TYPING_TOOL)\b")
    # the implementation block + the YdotoolInjector body legitimately name them
    impl_start = next(i for i, l in enumerate(src) if l.startswith("_TYPING_TOOL = None"))
    seam_start = next(i for i, l in enumerate(src)
                      if l.startswith("class YdotoolInjector("))
    seam_end = next(i for i, l in enumerate(src) if l.startswith("_INJECTION_METHODS"))
    strays = [(i + 1, l.strip()) for i, l in enumerate(src)
              if guarded.search(l) and not (impl_start <= i < seam_start or
                                            seam_start <= i < seam_end)]
    check("no stray direct injection calls", not strays, strays)

    # Restore-on-start must be SYNCHRONOUS and must run before the background
    # typing-init thread: a crash leaves NO input method, so recovery cannot
    # sit behind a daemon thread that may be scheduled arbitrarily late.
    joined = "\n".join(src)
    i_restore = joined.find('restore_prior_engine("service start")')
    i_thread = joined.find("threading.Thread(target=_init_typing")
    check("restore-on-start is present", i_restore != -1)
    check("restore-on-start runs BEFORE the _init_typing thread starts",
          i_restore != -1 and i_thread != -1 and i_restore < i_thread,
          f"restore@{i_restore} thread@{i_thread}")
    check("restore-on-start is not buried inside prepare()",
          "def prepare" not in joined[max(0, i_restore - 400):i_restore])
    print(f"       (impl block lines {impl_start+1}-{seam_start}, "
          f"seam lines {seam_start+1}-{seam_end})")

    # ---- 6. the typing-engine spell -------------------------------------
    # "cast typing engine" must (a) always leave IBus -- including from
    # "auto", which may already be on IBus -- and (b) describe the backend
    # get_injector() actually built, never the config string it just wrote:
    # an "ibus" request falls back to ydotool when IBus is unreachable, and
    # that is precisely the state where a key CAN stick (#56).
    # _save_config_flag() writes CONFIG_PATH, so point it at a temp file.
    print("\n[6] typing-engine spell (injection_toggle)")
    import json
    import shutil
    import tempfile

    class Svc:  # just the two methods the op needs, bound to a bare object
        _save_config_flag = mod.GnomeSpeaksService._save_config_flag
        _spell_ctx_dbus = mod.GnomeSpeaksService._spell_ctx_dbus

    svc = Svc()
    real_path = mod.CONFIG_PATH
    real_home = os.path.expanduser("~/.config/speech-to-cli/config.json")
    home_before = open(real_home, "rb").read() if os.path.exists(real_home) else None
    tmpdir = tempfile.mkdtemp(dir=_STATE)
    mod.CONFIG_PATH = os.path.join(tmpdir, "config.json")

    def toggle(start):
        if start is None:
            mod.CONFIG.pop("injection_method", None)
        else:
            mod.CONFIG["injection_method"] = start
        mod._injector = None
        mod._injector_method = None
        reply = svc._spell_ctx_dbus("injection_toggle")
        return reply or "", mod.CONFIG.get("injection_method"), mod.get_injector()

    def on_disk():
        with open(mod.CONFIG_PATH) as f:
            return json.load(f).get("injection_method")

    try:
        mod.IbusInjector = StubIbus

        StubIbus.reachable = True
        reply, cfg, inj = toggle("ydotool")
        check("ydotool -> ibus: config now 'ibus'", cfg == "ibus", cfg)
        check("ydotool -> ibus: written to CONFIG_PATH", on_disk() == "ibus", on_disk())
        check("ydotool -> ibus: backend is IBus", type(inj) is StubIbus, type(inj))
        check("ydotool -> ibus: reply says input method",
              "input method" in reply and "no key can stick" in reply, repr(reply))

        reply, cfg, inj = toggle("ibus")
        check("ibus -> ydotool: config now 'ydotool'", cfg == "ydotool", cfg)
        check("ibus -> ydotool: backend is ydotool",
              type(inj) is mod.YdotoolInjector, type(inj))
        check("ibus -> ydotool: reply says virtual keyboard",
              "virtual keyboard" in reply and "no key can stick" not in reply, repr(reply))

        reply, cfg, inj = toggle("auto")
        check("auto -> ydotool (escape hatch works from auto)", cfg == "ydotool", cfg)
        check("auto -> ydotool: backend is ydotool",
              type(inj) is mod.YdotoolInjector, type(inj))
        check("auto -> ydotool: reply says virtual keyboard",
              "virtual keyboard" in reply and "input method" not in reply, repr(reply))

        reply, cfg, inj = toggle(None)
        check("unset -> ibus", cfg == "ibus" and type(inj) is StubIbus, (cfg, type(inj)))
        reply, cfg, inj = toggle("uinput")
        check("unknown value (== ydotool) -> ibus", cfg == "ibus", cfg)

        StubIbus.reachable = False
        reply, cfg, inj = toggle("ydotool")
        check("ibus unreachable: config still records the 'ibus' request", cfg == "ibus", cfg)
        check("ibus unreachable: backend fell back to ydotool",
              type(inj) is mod.YdotoolInjector, type(inj))
        check("ibus unreachable: reply does NOT claim 'no key can stick'",
              "no key can stick" not in reply, repr(reply))
        check("ibus unreachable: reply says so",
              "unreachable" in reply and "virtual keyboard" in reply, repr(reply))

        mod.IbusInjector = None
        reply, cfg, inj = toggle("ydotool")
        check("no IBus backend at all: reply does NOT claim 'no key can stick'",
              "no key can stick" not in reply and "virtual keyboard" in reply, repr(reply))

        home_after = open(real_home, "rb").read() if os.path.exists(real_home) else None
        check("the real ~/.config/speech-to-cli/config.json was not touched",
              home_after == home_before)
    finally:
        mod.IbusInjector = real_ibus_cls
        mod._injector = None
        mod._injector_method = None
        mod.CONFIG_PATH = real_path
        mod.CONFIG.pop("injection_method", None)
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("ALL SEAM CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
