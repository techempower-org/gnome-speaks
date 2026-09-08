#!/usr/bin/env python3
"""repro: shutdown() ran TWICE on every service stop (#177).

main() calls `service.shutdown()` from the SIGTERM handler and again from
`loop.run()`'s `finally`. Measured 2026-09-08 in the journal: 36 "Shutting
down" for 18 stops, and each run re-did the teardown -- `get_injector()
.recover()` twice, which is two IBus global-engine probes (each one libibus
"No global engine" warning) and two ydotoold resets per stop.

  S1  shutdown() twice -> recover() once, "Shutting down" logged once
  S2  guard: the first call still does the full teardown (recover ran, the
      shutting-down flag is set)

Exit 0 = shutdown is idempotent; 1 = the double teardown is present;
2 = setup failure.  Baseline: RED on a715022 (S1: recover=2, logged twice).
"""
import logging
import sys

import harness

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} ({label}) {detail}")
    if not cond:
        FAILS.append(label)


class CountingInjector:
    name = "fake"

    def __init__(self):
        self.recovers = 0

    def recover(self):
        self.recovers += 1

    def end(self):
        pass

    def cancel(self):
        pass


def main():
    mod, _events = harness.load()
    svc = harness.make_service(mod)
    inj = CountingInjector()
    mod.get_injector = lambda: inj
    mod._discard_prewarmed_rec = lambda *a, **k: None
    mod._invalidate_stt_ws = lambda *a, **k: None

    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())
    h = H(level=logging.INFO)
    mod.log.addHandler(h)
    try:
        svc.shutdown()
        first_recovers = inj.recovers
        svc.shutdown()
    finally:
        mod.log.removeHandler(h)

    logged = sum(1 for m in records if m == "Shutting down")
    print(f"  measured: recover() calls after two shutdown()s = {inj.recovers}, "
          f"'Shutting down' logged {logged}x")
    print("[S2] the first shutdown() does the whole teardown")
    check("S2 recover ran on the first call", first_recovers == 1, first_recovers)
    check("S2 shutting-down flag set", getattr(svc, "_shutting_down", None) is True)
    print("[S1] a second shutdown() is a no-op")
    check("S1 recover() ran exactly once", inj.recovers == 1, inj.recovers)
    check("S1 'Shutting down' logged exactly once", logged == 1, logged)

    if FAILS:
        print(f"FAILURES: {len(FAILS)}")
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
