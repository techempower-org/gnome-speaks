"""C1: the dispatcher checks its hold conditions BEFORE dequeuing and never
re-checks, so anything that arrives while it is parked in get(timeout=0.2)
is spoken over an open mic.

Schedule:
  t0  queue empty, state idle -> dispatcher is parked inside
      self._tts_queue.get(timeout=0.2)
  t1  mic opens (start_listening / loop restart / _drain_speech_gap tail):
      state -> "listening"
  t2  an agent POSTs /speak: put_nowait wakes the parked get(), which returns
      the item and plays it. The gate is never consulted again.

The window is up to 200ms wide and reopens every 200ms, so a narrator agent
plus a dictation keypress lands in it routinely. Each attempt below is one
draw; the loop reports how many of N attempts spoke over the "open mic".
"""
import sys
import time

# No sys.path insert into a world-writable /tmp dir: those dirs held no
# harness.py (the line was already a no-op), but a stale copy left there
# would silently shadow this suite's own. See harness.py's ISOLATION note.
import harness

mod, events = harness.load(fake_tts_seconds=0.3)
svc = harness.make_service(mod)
time.sleep(0.3)

ATTEMPTS = 12
fired = 0
for i in range(ATTEMPTS):
    events.clear()
    # Mic opens, then an agent posts speech a hair later.
    svc._set_state("listening")
    time.sleep(0.005)
    item_id, pos, _ = svc.enqueue_speech(f"A{i} narrating", source="agent-x")
    time.sleep(0.25)
    spoke_over_mic = any(k == "start" for k, _t, _ts in events) and \
        svc.current_state in ("listening", "speaking")
    started = [t for k, t, _ in events if k == "start"]
    if started:
        fired += 1
        print(f"attempt {i}: SPOKE while mic open -> {started}")
    # reset for the next draw
    svc._set_state("idle")
    time.sleep(0.35)
    svc._drain_tts_queue()

print(f"\nRESULT: {fired}/{ATTEMPTS} attempts played queued speech "
      f"while state == 'listening'")
sys.exit(1 if fired else 0)
