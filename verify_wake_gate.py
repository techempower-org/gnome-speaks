#!/usr/bin/env python3
"""Verify the wake-word secure gate (spec §4.3, #55) without a desktop.

Runs against the real modules with the IBus daemon, the microphone and the
speech queue all mocked out. Exit code 0 = every check passed.

    python3 verify_wake_gate.py
"""
import importlib.util
import os
import re
import sys
import threading
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
inj._engine = engine(FREE_FORM, focused=False)
check("not focused -> unknown", inj.purpose_known() is False)
inj._engine = engine(FREE_FORM)
check("FREE_FORM (0) -> allowed", inj.purpose_known() is True)
inj._engine = engine(EMAIL)
check("EMAIL (6) -> allowed", inj.purpose_known() is True)
inj._engine = engine(PASSWORD)
check("PASSWORD (8) -> refused", inj.purpose_known() is False)
inj._engine = engine(PIN)
check("PIN (9) -> refused", inj.purpose_known() is False)

# The gate FAILS OPEN, and that is the property worth pinning down: a client
# that never calls SetContentType is delivered to the engine as (0, 0), which
# is bit-for-bit a declared FREE_FORM.  ibus-daemon 1.5.34 forwards the content
# type to a fresh engine unconditionally, so "SetContentType never arrived" is
# not a state a focused engine can be in -- the old row asserting it was dead
# weight.  What is real is that the two are indistinguishable:
undeclared = engine(0)          # client declared nothing -> daemon sends (0,0)
declared_free_form = engine(0)  # client declared FREE_FORM -> also (0,0)
check("undeclared field is indistinguishable from FREE_FORM",
      (undeclared.purpose, undeclared.hints)
      == (declared_free_form.purpose, declared_free_form.hints))
inj._engine = undeclared
check("undeclared field is ALLOWED (documented fail-open)", inj.purpose_known() is True)

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
    # start_listening reads _stt_lock/_stt_thread before its early return
    # (#63: "stop, it didn't stop, press again" guard), so the stand-in has to
    # carry a real lock or it dies with AttributeError before the assertion.
    self = types.SimpleNamespace(_wake_initiated=start_wake,
                                 _stt_lock=threading.Lock(),
                                 _stt_thread=BusyThread())
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
check("a blocked session never live-types",
      re.search(r"if wake_blocked:\n\s+live_typing = False", cycle) is not None)
# Mode scoping: conversation mode types nothing at the cursor, so it must not
# even ask -- an unknown field is none of its business, and asking would speak
# a refusal into an LLM turn.
# A missing marker must FAIL, not raise: on the parent commit none of these
# exist, and a traceback would hide every check after it.
_gate_start = cycle.find("wake_blocked = (")
_gate_end = cycle.find("use_lexical =", _gate_start if _gate_start >= 0 else 0)
gate_expr = cycle[_gate_start:_gate_end] if _gate_start >= 0 <= _gate_end else ""
check("the gate is never evaluated in conversation mode",
      'not CONFIG.get("conversation_mode", False)' in gate_expr
      and 'CONFIG.get("dictation_mode", True)' in gate_expr)
# Two call sites in the whole service and no more: the streaming cycle (once
# per session) and the batch/REST worker (once per utterance, no live typing
# to gate).  A third would mean the per-path gating of #55 is creeping back.
check("exactly two gate call sites in the service", src.count("self._wake_gate_blocks()") == 2)
batch = src[src.index("def _batch_stt_worker"):src.index("def _streaming_stt_cycle")]
check("the batch worker gates inside its dictation branch, after conversation mode",
      batch.count("self._wake_gate_blocks()") == 1
      and batch.index('if CONFIG.get("conversation_mode", False):')
      < batch.index("self._wake_gate_blocks()"))
check("the final dictation branch honours the session verdict",
      re.search(r"if CONFIG\.get\(\"dictation_mode\", True\):\n\s+if wake_blocked:", cycle)
      is not None)

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print("all checks passed")
