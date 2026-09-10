#!/usr/bin/env python3
"""repro: a wake-opened session loops only while it hears words (2026-09-10).

The wake model has a false-positive rate -- measured twice in 13 minutes off a
webcam mic on 2026-09-10 -- and `continuous_dictation` is persisted on disk.
So a false positive opened the mic, the loop re-entered listening after every
quiet cycle (#166 made silence continue the loop, correctly, for the hotkey),
and a private phone call was typed through ydotool until someone found a stop.
Hands-free semantics have to be: keep listening while there are words, close
the mic on the first clean quiet cycle. The Loop flag is untouched -- the
hotkey keeps full loop semantics (c10).

Batch (vad) path, driven exactly like c10:
  W1  wake session, loop on, [text, quiet]      -> text typed, then the quiet
      cycle CLOSES the mic: 2 cycles, idle, no Error, flag still True
  W2  wake session, loop on, [quiet]            -> 1 cycle, idle, nothing typed
  W3  control -- HOTKEY session, loop on, [quiet, quiet, quiet, text]
                                                -> loops through the silences
      and is still listening (c10 L1), so the verdict is the wake mark, not
      a broken loop
  W4  wake session, loop on, [text, text, quiet] -> both typed, 3 cycles, idle
      (words keep the hands-free session open; silence ends it)
  W5  the streaming cycle reads the wake mark too: its quiet window and its
      quiet-cycle exit both consult `_wake_initiated` (static -- the streaming
      loop has no in-process driver here; the batch path is the one every
      speech_backend=local session takes)

Baseline 7be33ed: W1 does not settle (the wake session loops through the
quiet cycle like a hotkey one), W2 likewise, W5 finds no reader.

Run:  GS_SVC_PATH=<gnome-speaks-service.py> python3 repro_c13_wake_quiet.py
Exit 0 = all hold; 1 = a verdict failed; 2 = setup failure.
"""
import inspect
import sys
import time

import harness
import repro_c10_batch_loop_silence as c10

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} ({label}) {detail}")
    if not cond:
        FAILS.append(label)


def run_batch(mod, script, wake, loop_on=True, window=c10.SETTLE):
    """c10.run_batch with the session opened by the wake word (wake=True) or
    the hotkey (wake=False)."""
    calls = [0]
    inj = c10._Inj()
    mod.get_injector = lambda: inj
    mod.HAS_VAD = True
    mod.GLib.idle_add = lambda fn, *a, **k: fn(*a, **k)
    holder = {}

    def fake_dispatch(**kw):
        i = calls[0]
        calls[0] += 1
        ctx = {"svc": holder["svc"], "kw": kw}
        if i >= len(script):
            return c10.quiet(ctx)
        step = script[i]
        return step(ctx) if callable(step) else step
    mod.stt_dispatch = fake_dispatch

    svc, errors = c10.fresh_service(mod, loop_on)
    holder["svc"] = svc
    rc = svc.start_listening(quick=True, wake=True) if wake else svc.start_listening()
    if rc != "ok":
        raise RuntimeError(f"start_listening refused: {rc}")
    settled = c10.settle(svc, timeout=window)
    states = set()
    for _ in range(10):
        states.add(svc.current_state)
        time.sleep(0.02)
    if not settled:
        svc.stop()
        t = svc._stt_thread
        if t is not None:
            t.join(timeout=2.0)
    return dict(cycles=calls[0], errors=errors, typed=inj.typed, settled=settled,
                states=states, flag=mod.CONFIG.get("continuous_dictation"),
                wake_mark=getattr(svc, "_wake_initiated", None))


def main():
    try:
        mod, _events = harness.load()
    except Exception as exc:
        print(f"!! SETUP FAILURE: cannot load service: {exc}")
        return 2
    print(f"service under test: {harness.SVC_PATH}")

    r = run_batch(mod, [c10.text("hello there"), c10.quiet], wake=True)
    check("W1", r["settled"] and r["cycles"] == 2 and r["typed"] == ["hello there"]
          and r["errors"] == [] and r["flag"] is True and r["wake_mark"] is True,
          f"cycles={r['cycles']} typed={r['typed']!r} errors={r['errors']!r} "
          f"settled={r['settled']} flag={r['flag']} wake_mark={r['wake_mark']}")

    r = run_batch(mod, [c10.quiet], wake=True)
    check("W2", r["settled"] and r["cycles"] == 1 and r["typed"] == [] and r["errors"] == [],
          f"cycles={r['cycles']} typed={r['typed']!r} errors={r['errors']!r} settled={r['settled']}")

    r = run_batch(mod, [c10.quiet, c10.quiet, c10.quiet, c10.text("hello there")],
                  wake=False, window=c10.WINDOW)
    check("W3", not r["settled"] and r["cycles"] >= 5 and r["typed"] == ["hello there"]
          and r["errors"] == [] and "listening" in r["states"] and r["wake_mark"] is False,
          f"cycles={r['cycles']} typed={r['typed']!r} settled={r['settled']} "
          f"states={sorted(r['states'])} wake_mark={r['wake_mark']}")

    r = run_batch(mod, [c10.text("first"), c10.text("second"), c10.quiet], wake=True)
    check("W4", r["settled"] and r["cycles"] == 3 and r["typed"] == ["first", "second"]
          and r["errors"] == [],
          f"cycles={r['cycles']} typed={r['typed']!r} errors={r['errors']!r} settled={r['settled']}")

    src = inspect.getsource(mod.GnomeSpeaksService._streaming_stt_cycle)
    n = src.count("_wake_initiated")
    check("W5", n >= 2, f"streaming cycle reads _wake_initiated at {n} site(s) (need >= 2: "
                        f"quiet window + quiet-cycle exit)")

    if FAILS:
        print(f"FAIL: {len(FAILS)} verdict(s) -- a wake-opened session inherits the persisted "
              f"loop and keeps the mic open through silence: {FAILS}")
        return 1
    print("PASS: a wake-opened session loops while it hears words and closes the mic on a "
          "quiet cycle; the hotkey loop is unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
