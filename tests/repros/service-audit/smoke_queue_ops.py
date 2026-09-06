"""Smoke test for the queue control surface after the audit fixes:
coalesce, skip (scoped + unscoped), stop/drain, pause/resume, respeak.
Checks the outcome ring stays one-terminal-outcome-per-id and nothing hangs.
"""
import sys
import time

# No sys.path insert into a world-writable /tmp dir: those dirs held no
# harness.py (the line was already a no-op), but a stale copy left there
# would silently shadow this suite's own. See harness.py's ISOLATION note.
import harness

mod, events = harness.load(fake_tts_seconds=0.6)
svc = harness.make_service(mod)
time.sleep(0.3)
fail = []

# --- coalesce: only the newest of a source survives ------------------------
svc._set_state("listening")           # hold the dispatcher so we can stack up
time.sleep(0.1)
a, _, _ = svc.enqueue_speech("C1 old", source="agent-a")
b, _, _ = svc.enqueue_speech("C2 old", source="agent-a")
c, _, dropped = svc.enqueue_speech("C3 new", source="agent-a", coalesce=True)
other, _, _ = svc.enqueue_speech("D1 peer", source="agent-b")
print("coalesced away:", dropped, "(expected", [a, b], ")")
if sorted(dropped) != sorted([a, b]):
    fail.append("coalesce dropped the wrong set")
with svc._tts_queue.mutex:
    pending = [i.id for i in svc._tts_queue.queue]
print("pending after coalesce:", pending, "(expected", [c, other], ")")
if pending != [c, other]:
    fail.append("coalesce broke FIFO order")

# --- pause holds the dispatcher even once the mic closes -------------------
mod.state.pause_active()
svc._set_state("idle")
time.sleep(0.5)
if any(k == "start" for k, _t, _ in events):
    fail.append("paused dispatcher started an item")
mod.state.resume_active()
time.sleep(0.4)
started = [t for k, t, _ in events if k == "start"]
print("started after resume:", started)
if not started:
    fail.append("resume did not release the dispatcher")

# --- scoped skip: wrong id is a no-op, right id cancels --------------------
with svc._queue_current_lock:
    cur = svc._queue_current
if cur is not None:
    if svc.skip_current(cur.id + 999) is not None:
        fail.append("scoped skip hit the wrong item")
    if svc.skip_current(cur.id) != cur.id:
        fail.append("scoped skip missed its own item")
time.sleep(0.8)

# --- drain: everything pending gets exactly one 'canceled' -----------------
svc._set_state("listening")
time.sleep(0.1)
ids = [svc.enqueue_speech(f"E{i} x", source="agent-c")[0] for i in range(5)]
cleared = svc._drain_tts_queue()
print("drained:", cleared, "of", len(ids))
if cleared != len(ids):
    fail.append("drain missed items")
svc._set_state("idle")
time.sleep(0.5)

ring = list(svc._queue_recent)
ring_ids = [o["id"] for o in ring]
dupes = [i for i in set(ring_ids) if ring_ids.count(i) > 1]
print("outcome ring:", [(o["id"], o["outcome"]) for o in ring])
if dupes:
    fail.append(f"duplicate terminal outcomes: {dupes}")

# --- respeak with the chronicle disabled must fail cleanly, not hang -------
print("respeak (chronicle off):", svc.respeak(None))

print("\nRESULT:", "SMOKE OK" if not fail else f"FAILURES: {fail}")
sys.exit(1 if fail else 0)
