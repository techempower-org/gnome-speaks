#!/usr/bin/env python3
"""Issue #21 consequence (b): a transcript is typed at the cursor AFTER stop.

_batch_stt_worker does:

    state._cancel_event.clear()
    result = stt_dispatch(mode=mode)
    if result.get("cancelled"): ... return
    user_text = result.get("text", "")
    ...
    get_injector().commit(user_text)          # <-- lands at the cursor

The ONLY cancellation signal it consults is `result["cancelled"]`, i.e. the
library's own verdict, and the library derives that verdict from the single
process-global wire `state._cancel_event`.  Two schedules defeat it:

  B1 -- cancel arrives after the library's last check.
        stt_fixed() (speech-to-cli stt.py:680) checks is_cancelled() exactly
        once, right after the recorder exits, and then does a POST to Azure
        with a 30 s timeout.  A /stop during that upload is never seen: the
        library returns the text and the service types it.  No un-cancel by
        anyone is required -- the window is the whole upload.

  B2 -- the audit's schedule: the cancel IS pending, but a later worker's
        `state._cancel_event.clear()` takes the wire down before the library
        reads it, so the library reports success and the service types it.
        Modelled here with a rendezvous so the interleaving is exact rather
        than hoped for; the un-cancel itself is the real svc.speak() path.

Both end the same way, and it is the one that matters most for a voice-first
user: text appears at the cursor after they asked for silence.

exit 0 = nothing reached the cursor after stop; exit 1 = it did.
"""
import sys
import threading
import time

import harness

SECRET = "this sentence must never reach the cursor"


def scenario_b1():
    """Cancel lands while the library is past its last cancellation check."""
    mod, _events, inj = harness.load()
    svc = harness.make_service(mod)
    svc._stt_mode = "fixed"

    upload_started = threading.Event()

    def fake_stt(mode=None, **kw):
        # phase 1 -- recorder running; cancel_active() terminates it and the
        # library then checks is_cancelled() once.
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            if mod.state._cancel_event.is_set():
                return {"text": "", "cancelled": True}
            time.sleep(0.01)
        if mod.state._cancel_event.is_set():
            return {"text": "", "cancelled": True}
        # phase 2 -- the Azure POST.  Nothing between here and the return
        # looks at the wire again (stt.py:680-700).
        upload_started.set()
        time.sleep(5.0)
        return {"text": SECRET}

    mod.stt_dispatch = fake_stt

    assert svc.start_listening() == "ok"
    assert upload_started.wait(5.0), "setup: upload phase never reached"

    t_stop = time.monotonic()
    svc.stop()                       # the user says "stop"
    print(f"    stop() returned after {time.monotonic() - t_stop:.1f}s "
          f"(state={svc.current_state})")

    harness.wait_for(lambda: inj.all_text(), 8.0)
    time.sleep(0.5)
    late = [(txt, ts - t_stop) for txt, ts in inj.all_text() if ts > t_stop]
    return late, svc.current_state


def scenario_b2():
    """A later worker's clear() un-cancels a still-running STT worker."""
    mod, _events, inj = harness.load()
    svc = harness.make_service(mod)
    svc._stt_mode = "fixed"

    about_to_check = threading.Event()
    may_check = threading.Event()

    def fake_stt(mode=None, **kw):
        # phase 1 -- recorder; cancel_active() kills it promptly.
        while not mod.state._cancel_event.is_set():
            time.sleep(0.01)
        # The library is now between "recorder exited" and its single
        # is_cancelled() call.  Hold there while the un-cancel happens.
        about_to_check.set()
        may_check.wait(10.0)
        if mod.state._cancel_event.is_set():
            return {"text": "", "cancelled": True}
        upload = time.monotonic()
        time.sleep(0.5)
        del upload
        return {"text": SECRET}

    mod.stt_dispatch = fake_stt

    assert svc.start_listening() == "ok"
    time.sleep(0.2)

    t_stop = time.monotonic()
    svc.stop()                       # sets the wire; join times out at 3 s
    print(f"    stop() returned after {time.monotonic() - t_stop:.1f}s "
          f"(state={svc.current_state})")
    assert about_to_check.wait(5.0), "setup: library never reached its check"

    # The real un-cancel: any subsequent speech worker clears the global wire
    # on entry (gnome-speaks-service.py, _speak_worker).
    svc.speak("unrelated agent chatter")
    harness.wait_for(lambda: not mod.state._cancel_event.is_set(), 3.0)
    print(f"    after a later speak() the wire is "
          f"{'STILL SET' if mod.state._cancel_event.is_set() else 'CLEARED'}")
    may_check.set()

    harness.wait_for(lambda: inj.all_text(), 8.0)
    time.sleep(0.5)
    late = [(txt, ts - t_stop) for txt, ts in inj.all_text() if ts > t_stop]
    mod.state.cancel_active()
    return late, None


def main():
    print(f"service under test: {harness.SVC_PATH}")
    failures = 0
    for name, fn, desc in (
            ("B1", scenario_b1, "cancel arrives after the library's last check"),
            ("B2", scenario_b2, "a later worker's clear() un-cancels the STT worker")):
        print(f"\n  {name}: {desc}")
        late, state_after = fn()
        if late:
            failures += 1
            for txt, dt in late:
                print(f"    FAIL: {txt!r} reached the cursor {dt:.1f}s AFTER stop()")
        else:
            print("    OK: nothing reached the cursor after stop()")
        if state_after is not None and state_after not in ("idle",):
            print(f"    note: service left in state {state_after!r} after stop()")

    print()
    if failures:
        print(f"FAIL: {failures}/2 schedules typed a transcript after stop.")
        return 1
    print("PASS: a stopped STT operation never reaches the cursor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
