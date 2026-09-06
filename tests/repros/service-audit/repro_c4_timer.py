"""C4: _reset_inactivity_timer read-remove-adds an unsynchronized source id
from every _set_state, i.e. from concurrent worker threads.

Two threads that read the same id both remove it (the second gets a GLib
"Source ID N was not found" warning) and then both store their own, orphaning
a live 10-minute timer. Every orphan eventually fires; a fire that finds the
service idle calls _main_loop.quit(), so the service exits while in use. A
fire that finds it busy reschedules AND clobbers the tracked id -> more
orphans.

Counts timers that are still alive in the main context but no longer tracked.
"""
import sys
import threading
import time

# No sys.path insert into a world-writable /tmp dir: those dirs held no
# harness.py (the line was already a no-op), but a stale copy left there
# would silently shadow this suite's own. See harness.py's ISOLATION note.
import harness

mod, _events = harness.load(0.05)
from gi.repository import GLib  # noqa: E402

svc = harness.make_service(mod)
loop = GLib.MainLoop()
svc._main_loop = loop
threading.Thread(target=loop.run, daemon=True).start()
time.sleep(0.2)
svc._reset_inactivity_timer()

seen = set()
stop = threading.Event()


def churn(a, b):
    while not stop.is_set():
        svc._set_state(a)
        svc._set_state(b)
        seen.add(svc._inactivity_source_id)


t1 = threading.Thread(target=churn, args=("idle", "speaking"), daemon=True)
t2 = threading.Thread(target=churn, args=("speaking", "idle"), daemon=True)
t1.start()
t2.start()
time.sleep(3)
stop.set()
time.sleep(0.3)

ctx = GLib.MainContext.default()
tracked = svc._inactivity_source_id
orphans = [i for i in seen
           if i is not None and i != tracked and ctx.find_source_by_id(i)]
print(f"distinct ids observed: {len(seen)}")
print(f"tracked id: {tracked}")
print(f"ORPHANED live timers: {len(orphans)}")
loop.quit()
print("RESULT:", "BUG REPRODUCED" if orphans else "no orphans")
sys.exit(1 if orphans else 0)
