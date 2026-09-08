#!/usr/bin/env python3
"""repro: a batch/offline STT result of {"error": ...} reaches the user (#130).

speech-to-cli's stt_vad()/stt_fixed() return {"error": "Azure STT unreachable
(...); offline fallback failed"} -- no "text" key -- when Azure is down AND the
Wyoming fallback fails. _deliver_stt_result read only result["text"], so a
total STT failure logged "No speech detected", emitted TranscriptionReady("")
and idled with NO toast. With speech_backend=local every streaming dictation is
routed to this batch path, so the swallow was on the PRIMARY offline path.

  E1  an error dict emits exactly one Error signal naming the failure
  E2  ... and still emits TranscriptionReady("") and returns to idle
  E3  ... and never types or clipboards anything
  E4  control: a text-present result types the text and emits NO Error
  E5  control: an empty-text result (genuine silence) emits NO Error

Run:  GS_SVC_PATH=<gnome-speaks-service.py> python3 repro_c8_batch_error_toast.py
Exit 0 = all hold; 1 = a verdict failed; 2 = setup failure.
"""
import sys

import harness

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} ({label}) {detail}")
    if not cond:
        FAILS.append(label)


def deliver(mod, svc, result):
    """Run one result through _deliver_stt_result; return (idle_calls, typed)."""
    idle_calls, typed = [], []

    class _Inj:
        name = "fake"
        def commit(self, text): typed.append(text)
        def paste(self, text): typed.append(text)
        def end(self): pass
    mod.get_injector = lambda: _Inj()
    mod.clipboard_write = lambda text: typed.append(text) or True

    def rec_idle_add(fn, *args, **kw):
        idle_calls.append((getattr(fn, "__name__", str(fn)), args))
        return 0
    real_idle_add = mod.GLib.idle_add
    mod.GLib.idle_add = rec_idle_add
    try:
        svc._set_state("listening")
        tok = svc._cancels.issue("stt")
        assert svc._cancels.begin(tok)
        try:
            svc._deliver_stt_result(result, "vad", tok)
        finally:
            svc._cancels.retire(tok)
    finally:
        mod.GLib.idle_add = real_idle_add
    return idle_calls, typed


def main():
    try:
        mod, _events = harness.load()
        svc = harness.make_service(mod)
    except Exception as exc:
        print(f"!! SETUP FAILURE: cannot load service: {exc}")
        return 2
    svc._save_config_flag = lambda *a, **k: None
    svc._reload_config_flags = lambda *a, **k: None
    svc._wake_gate_blocks = lambda: False
    mod.CONFIG["dictation_mode"] = True
    mod.CONFIG["conversation_mode"] = False
    mod.CONFIG["continuous_dictation"] = False

    msg = "Azure STT unreachable (ConnectionError); offline fallback failed"
    calls, typed = deliver(mod, svc, {"error": msg})
    errors = [a[0] for n, a in calls if n == "_emit_error"]
    ready = [a[0] for n, a in calls if n == "_emit_transcription_ready"]
    check("E1", len(errors) == 1 and msg in errors[0],
          f"Error signals={errors!r}")
    check("E2", ready == [""] and svc._state == "idle",
          f"TranscriptionReady={ready!r} state={svc._state}")
    check("E3", typed == [], f"typed={typed!r}")

    calls, typed = deliver(mod, svc, {"text": "hello there"})
    errors = [a[0] for n, a in calls if n == "_emit_error"]
    check("E4", typed == ["hello there"] and errors == [],
          f"typed={typed!r} errors={errors!r}")

    calls, typed = deliver(mod, svc, {"text": "", "status": "NoAudio"})
    errors = [a[0] for n, a in calls if n == "_emit_error"]
    ready = [a[0] for n, a in calls if n == "_emit_transcription_ready"]
    check("E5", errors == [] and ready == [""] and typed == [],
          f"errors={errors!r} ready={ready!r}")

    if FAILS:
        print(f"FAIL: {len(FAILS)} verdict(s) -- a batch STT error dict is swallowed as silence")
        return 1
    print("PASS: a batch STT error reaches the user as an Error signal; text and silence unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
