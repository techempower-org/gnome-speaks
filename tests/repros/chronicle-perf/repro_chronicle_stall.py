"""Issue #22 repro: GetChronicle scans the whole ledger ON the GLib main loop.

Three measurements, all against a synthetic ledger (never the real one — the
harness redirects XDG_STATE_HOME to /tmp):

  1. main-loop stall — a 20ms heartbeat runs on a real GLib main loop while
     DBusHandler.handle_method_call("GetChronicle") is dispatched exactly as
     GDBus dispatches it. The worst heartbeat gap IS the stall: for that long
     the service emits no SubtitleUpdate, no AudioLevel, no StateChanged.
  2. scan cost — _chronicle_read(limit=12), the badge/submenu call.
  3. append starvation — how long _chronicle_append blocks behind a reader
     that holds _chronicle_lock for a whole-file scan. That is the speech
     path waiting on the UI.

Ledger sizing comes from the real one: 369 entries / 142 KB = ~385 B/entry,
growing ~74 entries/day, so a year of use is ~27k entries / ~10 MB.
"""
import json
import os
import statistics
import sys
import threading
import time

# No sys.path insert into a world-writable /tmp dir: those dirs held no
# harness.py (the line was already a no-op), but a stale copy left there
# would silently shadow this suite's own. See harness.py's ISOLATION note.
import harness

ENTRIES = int(os.environ.get("LEDGER_ENTRIES", "40000"))

mod, _events = harness.load(0.05)
from gi.repository import GLib  # noqa: E402


def build_ledger(path, n):
    """Synthetic ledger with realistic entry shape (~385 B/line). No real text."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    filler = ("the queue held while the mic was open and the narrator waited "
              "for its turn to speak ")
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            body = f"synthetic entry {i} " + filler * 3
            f.write(json.dumps({
                "kind": "spoken" if i % 2 else "you",
                "text": body[:340],
                "ts": "2026-08-17T12:00:00-0700",
                "voice": "en-US-BrianNeural",
                "source": "agent-x",
                "id": 1755400000000 + i,
            }, ensure_ascii=False) + "\n")
    return os.path.getsize(path)


size = build_ledger(mod.CHRONICLE_PATH, ENTRIES)
print(f"ledger: {ENTRIES} entries, {size / 1e6:.1f} MB "
      f"({size / ENTRIES:.0f} B/entry) at {mod.CHRONICLE_PATH}")

svc = harness.make_service(mod)
handler = mod.DBusHandler(svc)


class FakeInvocation:
    """Stands in for Gio.DBusMethodInvocation."""

    def __init__(self):
        self.done = threading.Event()
        self.error = None

    def return_value(self, variant):
        self.payload = variant
        self.done.set()

    def return_dbus_error(self, name, msg):
        self.error = (name, msg)
        self.done.set()


# --- 1. main-loop stall ----------------------------------------------------
loop = GLib.MainLoop()
svc._main_loop = loop
beats = []


def heartbeat():
    beats.append(time.monotonic())
    return True


GLib.timeout_add(20, heartbeat)
inv = FakeInvocation()


def fire():
    # Exactly how GDBus dispatches an incoming call: on the main loop.
    handler.handle_method_call(None, None, mod.OBJECT_PATH, mod.INTERFACE_NAME,
                              "GetChronicle", GLib.Variant("(i)", (12,)), inv)
    return False


def finish():
    if inv.done.wait(timeout=25):
        GLib.idle_add(loop.quit)
    return False


GLib.timeout_add(500, fire)
threading.Thread(target=finish, daemon=True).start()
GLib.timeout_add(24000, lambda: (loop.quit(), False)[-1])
loop.run()

gaps = [round((b - a) * 1000, 1) for a, b in zip(beats, beats[1:])]
stall = max(gaps) if gaps else 0.0
entries = json.loads(inv.payload.unpack()[0]) if inv.error is None else []
print(f"1. main-loop stall: worst heartbeat gap {stall:.1f} ms "
      f"(median {statistics.median(gaps):.1f} ms over {len(gaps)} beats)")
print(f"   GetChronicle returned {len(entries)} entries, "
      f"oldest-first={bool(entries) and entries[0]['id'] < entries[-1]['id']}")

# --- 2. scan cost ----------------------------------------------------------
t = time.monotonic()
got = mod._chronicle_read(limit=12)
scan_ms = (time.monotonic() - t) * 1000
print(f"2. _chronicle_read(limit=12): {scan_ms:.1f} ms for {len(got)} entries")

t = time.monotonic()
found = mod._chronicle_find(1755400000000 + ENTRIES - 3)
find_ms = (time.monotonic() - t) * 1000
print(f"2b. _chronicle_find(recent id): {find_ms:.1f} ms, hit={found is not None}")

# --- 3. append starvation --------------------------------------------------
blocked = []
go = threading.Event()


def reader():
    go.set()
    for _ in range(3):
        mod._chronicle_read(limit=12)


r = threading.Thread(target=reader)
r.start()
go.wait()
time.sleep(0.002)  # let the reader take the lock
t = time.monotonic()
mod._chronicle_append("spoken", "a line the speech path needs to record")
blocked.append((time.monotonic() - t) * 1000)
r.join()
print(f"3. append blocked behind a reader: {blocked[0]:.1f} ms")

verdict = stall > 50 or scan_ms > 50 or blocked[0] > 50
print("\nRESULT:", "SLOW (issue #22 reproduced)" if verdict else "FAST")
sys.exit(1 if verdict else 0)
