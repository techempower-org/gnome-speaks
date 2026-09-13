#!/usr/bin/env python3
"""repro: live transcript + live typing on the batch (VAD) route (2026-09-12).

With speech_backend=local every dictation is routed to _batch_stt_worker, which
called stt_dispatch() and typed the ONE final transcript -- so the badge's live
transcript and typing-as-you-speak, both streaming-path features, did not
exist on the local route. speech-to-cli now offers stt(partial_cb=); the
service must pass it, show each hypothesis, live-type it through the PINNED
backend, and reconcile the final against what was typed.

Driven like c10 (fake stt_dispatch on the STT thread), with a fake dispatch
that CALLS partial_cb with growing hypotheses before returning the final.

  V1  partial_cb is passed; every hypothesis reaches PartialTranscription and
      the injector via replace_text (old -> new); the final is reconciled:
      replace_text(last partial, final) then finalize(final); commit() is
      NEVER called (no second copy); the on-screen text equals the final
  V2  loop on: the separator type_text(" ") lands between replace and finalize
      on a non-preedit backend
  V3  a panic stop() mid-utterance: the live text is erased (backspaces ==
      len(last partial)), nothing finalized or committed
  V4  the final is a spell ("cast loop"): the live-typed incantation is erased
      BEFORE the cast runs, nothing finalized
  V5  conversation mode: partials still reach the badge, nothing is typed,
      no typer, no injector ops
  V6  _STT_HAS_PARTIAL_CB False (older speech-to-cli): no partial_cb kwarg,
      commit(final) exactly as before -- the guard
  V7  silence with a stray partial: erased, nothing committed

Baseline 735d815 (pre-seam service): V1/V2/V4 fail (partial_cb never passed,
final committed). exit 0 = all hold; 1 = a verdict failed; 2 = setup failure.
"""
import sys
import threading
import time

import harness
import repro_c10_batch_loop_silence as c10

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} ({label}) {detail}")
    if not cond:
        FAILS.append(label)


class OpsInj:
    """Records every injector op in order; models a key-event (non-preedit) backend
    whose screen text is tracked."""
    name = "ops"

    def __init__(self, preedit=False):
        self.ops = []
        self.screen = ""
        self._preedit = preedit

    def available(self):
        return True

    def supports_preedit(self):
        return self._preedit

    def acquire(self):
        return True

    def end(self):
        return True

    def cancel(self):
        return True

    def purpose_known(self):
        return False

    def replace_text(self, old, new):
        self.ops.append(("replace", old, new))
        assert self.screen.endswith(old), f"replace_text old={old!r} but screen={self.screen!r}"
        self.screen = self.screen[:len(self.screen) - len(old)] + new
        return True

    def send_backspaces(self, n):
        self.ops.append(("backspace", n))
        self.screen = self.screen[:len(self.screen) - n] if n else self.screen
        return True

    def type_text(self, t):
        self.ops.append(("type", t)); self.screen += t; return True

    def finalize(self, t):
        self.ops.append(("finalize", t)); return True

    def commit(self, t):
        self.ops.append(("commit", t)); self.screen += t; return True

    def paste(self, t):
        self.ops.append(("paste", t)); self.screen += t; return True


def run(mod, hyps, final, *, loop=False, conversation=False, stop_mid=False,
        preedit=False, has_partial=True, cast=False):
    inj = OpsInj(preedit=preedit)
    mod.get_injector = lambda: inj
    mod.HAS_VAD = True
    mod.GLib.idle_add = lambda fn, *a, **k: fn(*a, **k)
    mod._STT_HAS_PARTIAL_CB = has_partial
    seen = {"kwargs": None, "stopped": False}
    holder = {}

    def fake_dispatch(**kw):
        seen["kwargs"] = sorted(kw)
        cb = kw.get("partial_cb")
        for h in hyps:
            if cb:
                cb(h)
            time.sleep(0.03)
        if stop_mid:
            threading.Thread(target=holder["svc"].stop, daemon=True).start()
            seen["stopped"] = c10.wait_until(holder["svc"]._stop_event.is_set)
            time.sleep(0.05)
        return {"text": final}
    mod.stt_dispatch = fake_dispatch

    svc, errors = c10.fresh_service(mod, loop)
    holder["svc"] = svc
    mod.CONFIG["conversation_mode"] = conversation
    partials = []
    svc._throttled_partial_transcription = lambda t: partials.append(t)
    svc._is_cast = lambda text: cast
    casts = []
    svc._try_cast = lambda text: (casts.append(text) or True) if cast else False
    if conversation:
        svc._conversation_worker = lambda text, *a, **k: None
    rc = svc.start_listening()
    if rc != "ok":
        raise RuntimeError(f"start_listening refused: {rc}")
    c10.settle(svc, timeout=3.0)
    if loop:
        svc.stop()
        t = svc._stt_thread
        if t is not None:
            t.join(timeout=2.0)
    return dict(inj=inj, partials=partials, kwargs=seen["kwargs"], stopped=seen["stopped"],
                casts=casts, errors=errors)


def main():
    try:
        mod, _events = harness.load()
    except Exception as exc:
        print(f"!! SETUP FAILURE: cannot load service: {exc}")
        return 2
    print(f"service under test: {harness.SVC_PATH}")
    if not hasattr(mod, "_STT_HAS_PARTIAL_CB"):
        print("FAIL: service has no _STT_HAS_PARTIAL_CB -- the batch route passes no partial_cb; "
              "live transcript and live typing do not exist on the local route")
        return 1

    H = ["the", "the quick", "the quick brown"]
    F = "the quick brown fox"

    r = run(mod, H, F)
    ops = r["inj"].ops
    replaces = [o for o in ops if o[0] == "replace"]
    check("V1", r["kwargs"] and "partial_cb" in r["kwargs"] and r["partials"] == H
          and replaces[:3] == [("replace", "", "the"), ("replace", "the", "the quick"),
                               ("replace", "the quick", "the quick brown")]
          and ("replace", "the quick brown", F) in ops and ("finalize", F) in ops
          and not any(o[0] in ("commit", "paste") for o in ops) and r["inj"].screen == F,
          f"kwargs={r['kwargs']} partials={r['partials']} ops={ops} screen={r['inj'].screen!r}")

    r = run(mod, H, F, loop=True)
    ops = r["inj"].ops
    i_rep = next((i for i, o in enumerate(ops) if o == ("replace", "the quick brown", F)), None)
    i_sep = next((i for i, o in enumerate(ops) if o == ("type", " ")), None)
    i_fin = next((i for i, o in enumerate(ops) if o == ("finalize", F)), None)
    check("V2", None not in (i_rep, i_sep, i_fin) and i_rep < i_sep < i_fin
          and not any(o[0] == "commit" for o in ops),
          f"loop: ops={ops}")

    r = run(mod, H, F, stop_mid=True)
    ops = r["inj"].ops
    check("V3", r["stopped"] and ("backspace", len("the quick brown")) in ops
          and not any(o[0] in ("finalize", "commit", "paste") for o in ops)
          and r["inj"].screen == "",
          f"stop: ops={ops} screen={r['inj'].screen!r}")

    r = run(mod, ["cast", "cast loop"], "cast loop", cast=True)
    ops = r["inj"].ops
    check("V4", r["casts"] == ["cast loop"] and ("backspace", len("cast loop")) in ops
          and not any(o[0] in ("finalize", "commit", "paste") for o in ops)
          and r["inj"].screen == "",
          f"cast: casts={r['casts']} ops={ops} screen={r['inj'].screen!r}")

    r = run(mod, H, F, conversation=True)
    check("V5", r["partials"] == H and r["inj"].ops == [],
          f"conversation: partials={r['partials']} ops={r['inj'].ops}")

    r = run(mod, H, F, has_partial=False)
    ops = r["inj"].ops
    check("V6", r["kwargs"] is not None and "partial_cb" not in r["kwargs"]
          and ops == [("commit", F)] and r["partials"] == [],
          f"old library: kwargs={r['kwargs']} ops={ops}")

    r = run(mod, ["the"], "")
    ops = r["inj"].ops
    check("V7", ("backspace", 3) in ops and not any(o[0] in ("finalize", "commit") for o in ops)
          and r["inj"].screen == "",
          f"silence: ops={ops} screen={r['inj'].screen!r}")

    if FAILS:
        print(f"FAIL: {len(FAILS)} verdict(s) -- the batch route does not live-type or does not "
              f"reconcile the final: {FAILS}")
        return 1
    print("PASS: batch-route partials reach the badge and the pinned backend, the final is "
          "reconciled not re-typed, and cancel/cast/silence erase the live text")
    return 0


if __name__ == "__main__":
    sys.exit(main())
