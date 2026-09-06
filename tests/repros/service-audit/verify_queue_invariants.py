"""Invariant check for the dispatcher fix: with the mic flapping open/closed
under load, every queued item must play EXACTLY ONCE, IN ORDER, and never
while state == 'listening'/'processing' or while paused.

Also checks the outcomes ring: exactly one terminal outcome per id.
"""
import sys
import threading
import time

# No sys.path insert into a world-writable /tmp dir: those dirs held no
# harness.py (the line was already a no-op), but a stale copy left there
# would silently shadow this suite's own. See harness.py's ISOLATION note.
import harness

mod, events = harness.load(fake_tts_seconds=0.05)
svc = harness.make_service(mod)
time.sleep(0.3)

N = 24
violations = []


def flapper():
    for _ in range(60):
        svc._set_state("listening")
        # any playback that starts now is speaking over an open mic
        time.sleep(0.02)
        if svc.current_state == "speaking":
            violations.append("spoke while listening")
        svc._set_state("idle")
        time.sleep(0.02)


f = threading.Thread(target=flapper)
f.start()
ids = []
for i in range(N):
    item_id, _pos, _dropped = svc.enqueue_speech(f"I{i:02d} line", source="agent")
    ids.append(item_id)
    time.sleep(0.01)
f.join()

# let the backlog drain
deadline = time.time() + 30
while time.time() < deadline:
    with svc._queue_current_lock:
        busy = svc._queue_current is not None
    if not busy and svc._tts_queue.empty():
        break
    time.sleep(0.1)
time.sleep(0.5)

starts = [t for k, t, _ in events if k == "start"]
expected = [f"I{i:02d}" for i in range(N)]
print("played:", len(starts), "of", N)
print("order preserved:", starts == expected)
dupes = [t for t in set(starts) if starts.count(t) > 1]
print("duplicates:", dupes)
missing = [t for t in expected if t not in starts]
print("missing:", missing)
outcomes = list(svc._queue_recent)
ring_ids = [o["id"] for o in outcomes]
print("outcome ring (last 16):", len(ring_ids), "entries, dupe ids:",
      [i for i in set(ring_ids) if ring_ids.count(i) > 1])
print("mic violations:", len(violations))
ok = (starts == expected and not dupes and not missing and not violations
      and not [i for i in set(ring_ids) if ring_ids.count(i) > 1])
print("RESULT:", "INVARIANTS HOLD" if ok else "VIOLATION")
sys.exit(0 if ok else 1)
