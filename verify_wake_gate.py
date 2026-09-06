#!/usr/bin/env python3
"""Verify the wake-word secure gate (spec §4.3, #55) without a desktop.

Runs against the real modules with the IBus daemon, the microphone and the
speech queue all mocked out. Exit code 0 = every check passed.

    python3 verify_wake_gate.py
"""
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ibus_injector  # noqa: E402

failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def engine(purpose, focused=True, saw_content_type=True):
    """A stand-in for SpeaksEngine: only the fields purpose_known reads."""
    eng = types.SimpleNamespace(purpose=purpose, hints=0, focused=focused,
                                saw_content_type=saw_content_type)
    eng.is_secure = lambda: eng.purpose in ibus_injector._SECURE_PURPOSES
    return eng


# ── 1. purpose_known: FREE_FORM (0) is an ordinary field, not "unknown" ──────
inj = ibus_injector.IbusInjector()
FREE_FORM, PASSWORD, PIN, EMAIL = 0, 8, 9, 6

inj._engine = None
check("no engine object -> unknown", inj.purpose_known() is False)
inj._engine = engine(FREE_FORM, saw_content_type=False)
check("focused, SetContentType never arrived -> unknown", inj.purpose_known() is False)
inj._engine = engine(FREE_FORM, focused=False)
check("content-type seen but not focused -> unknown", inj.purpose_known() is False)
inj._engine = engine(FREE_FORM)
check("FREE_FORM (0) after SetContentType -> KNOWN non-secure", inj.purpose_known() is True)
inj._engine = engine(EMAIL)
check("EMAIL (6) -> known non-secure", inj.purpose_known() is True)
inj._engine = engine(PASSWORD)
check("PASSWORD (8) -> refused", inj.purpose_known() is False)
inj._engine = engine(PIN)
check("PIN (9) -> refused", inj.purpose_known() is False)

# ── 2. the service: verdict + wake mark across quick restarts ────────────────
spec = importlib.util.spec_from_file_location("gss", os.path.join(HERE, "gnome-speaks-service.py"))
gss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gss)
Service = gss.GnomeSpeaksService


class FakeInjector:
    def __init__(self, known):
        self.known = known
        self.acquired = self.ended = 0

    def acquire(self):
        self.acquired += 1
        return True

    def end(self):
        self.ended += 1

    def purpose_known(self):
        return self.known


def fake_service(wake, spoken):
    self = types.SimpleNamespace(_wake_initiated=wake)
    self._spell_speak = lambda text, voice=None: spoken.append(text)
    self._wake_gate_blocks = types.MethodType(Service._wake_gate_blocks, self)
    return self


def verdict(wake, gate_on, known):
    spoken = []
    fi = FakeInjector(known)
    gss.get_injector = lambda: fi
    gss.CONFIG["wake_word_secure_gate"] = gate_on
    blocked = fake_service(wake, spoken)._wake_gate_blocks()
    return blocked, spoken, fi


b, spoken, _ = verdict(wake=False, gate_on=True, known=False)
check("hotkey session is never gated", b is False and not spoken)
b, spoken, _ = verdict(wake=True, gate_on=False, known=False)
check("gate off -> wake session types", b is False and not spoken)
b, spoken, _ = verdict(wake=True, gate_on=True, known=True)
check("gate on, purpose known -> types", b is False and not spoken)
b, spoken, fi = verdict(wake=True, gate_on=True, known=False)
check("gate on, purpose unknown -> refused and spoken", b is True and len(spoken) == 1)
check("refusal hands the input method back", fi.ended == 1)


class BusyThread:
    def is_alive(self):
        return True

    def join(self, timeout=None):
        pass


def mark_after(start_wake, **kw):
    """Drive start_listening only as far as the wake mark (a live STT thread
    makes it return before touching audio or state)."""
    self = types.SimpleNamespace(_wake_initiated=start_wake, _stt_thread=BusyThread())
    Service.start_listening(self, **kw)
    return self._wake_initiated


check("wake=True marks the session", mark_after(False, wake=True) is True)
check("quick restart keeps the wake mark (loop / AI+Loop)", mark_after(True, quick=True) is True)
check("quick restart of a hotkey session stays unmarked", mark_after(False, quick=True) is False)
check("a fresh (hotkey) start clears the wake mark", mark_after(True) is False)

# ── 3. one verdict per streaming session, not one per paste ─────────────────
src = open(os.path.join(HERE, "gnome-speaks-service.py"), encoding="utf-8").read()
cycle = src[src.index("def _streaming_stt_cycle"):src.index("def enqueue_speech")]
check("streaming cycle computes wake_blocked next to live_typing",
      "wake_blocked = (" in cycle and cycle.index("wake_blocked = (") > cycle.index("live_typing = ("))
check("streaming cycle asks the gate exactly once", cycle.count("self._wake_gate_blocks()") == 1)
check("a blocked session never live-types", "if wake_blocked:\n            live_typing = False" in cycle)

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print("all checks passed")
