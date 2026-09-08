"""End-to-end: the two callers that ride along with the tail-read.

  - GET /chronicle over real HTTP (bound on port 0, an ephemeral port —
    never 7710, the live service owns that)
  - Respeak through DBusHandler.handle_method_call, which now goes to the
    thread pool: the reply must still arrive with the right value, and an id
    that has already rotated into chronicle.jsonl.1 must still be found and
    enqueued for playback.
"""
import http.client
import http.server
import json
import os
import shutil
import sys
import threading
import time

# No sys.path insert into a world-writable /tmp dir: those dirs held no
# harness.py (the line was already a no-op), but a stale copy left there
# would silently shadow this suite's own. See harness.py's ISOLATION note.
import harness

mod, events = harness.load(0.05)
from gi.repository import GLib  # noqa: E402

DIR = os.path.join(harness.SCRATCH, "endpoints")
shutil.rmtree(DIR, ignore_errors=True)
os.makedirs(DIR, exist_ok=True)
mod.CHRONICLE_PATH = os.path.join(DIR, "chronicle.jsonl")
mod._CHRONICLE_MAX_BYTES = 8192
mod._chronicle_last_id = 0
mod._chronicle_id_seeded = False
fails = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{'' if ok else '  <- ' + detail}")
    if not ok:
        fails.append(name)


svc = harness.make_service(mod)
for i in range(120):                      # forces at least one rotation
    mod._chronicle_append("spoken" if i % 2 else "you",
                          f"endpoint entry {i} " + "pad " * 20,
                          voice="en-US-BrianNeural", source="test")


def settle(timeout=10.0):
    """Wait out the background chronicle-rotate thread.

    _chronicle_rotate_locked() is DETECTION ONLY: it sets _chronicle_archiving
    and hands the archive+rename to a daemon thread (perf/chronicle-archive,
    PR #27). Asserting on the rotated file straight after the append loop
    races that thread, and the rename moves chronicle.jsonl aside without
    recreating it -- which is where the FileNotFoundError at the active-file
    read came from. Both were flaky on main long before anything read this
    file (measured: 1/12 green). Wait for the documented boundary instead.
    """
    deadline = time.monotonic() + timeout
    while mod._chronicle_archiving.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not mod._chronicle_archiving.is_set(), "rotation did not finish"


settle()
# Rotation renames the active generation away and does NOT recreate it -- the
# next append does. Restore a non-empty active generation so the "spans BOTH
# generations" checks below test what they mean to test.
for i in range(120, 130):
    mod._chronicle_append("spoken" if i % 2 else "you",
                          f"endpoint entry {i} " + "pad " * 20,
                          voice="en-US-BrianNeural", source="test")
settle()

TOTAL_APPENDED = 130   # 120 in the setup loop + 10 restoring the active file

rotated = mod.CHRONICLE_PATH + ".1"
check("rotated during setup", os.path.exists(rotated))

# ------------------------------------------------------------------ HTTP
mod.SpeechHTTPHandler.service = svc
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), mod.SpeechHTTPHandler)
port = srv.server_address[1]
assert port != 7710
threading.Thread(target=srv.serve_forever, daemon=True).start()


def get(path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    body = json.loads(r.read())
    c.close()
    return r.status, body


def post(path, payload):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = json.dumps(payload).encode()
    c.request("POST", path, body=body,
              headers={"Content-Type": "application/json"})
    r = c.getresponse()
    out = json.loads(r.read())
    c.close()
    return r.status, out


st, body = get("/chronicle?limit=12")
check("GET /chronicle 200", st == 200, str(st))
check("GET /chronicle returns 12", body.get("count") == 12, str(body.get("count")))
ids = [e["id"] for e in body["entries"]]
check("GET /chronicle oldest-first", ids == sorted(ids))
check("GET /chronicle reports enabled", body.get("enabled") is True)

st, body = get("/chronicle?limit=5&kind=you")
check("GET /chronicle?kind=you filters",
      st == 200 and all(e["kind"] == "you" for e in body["entries"])
      and body["count"] == 5, json.dumps(body)[:120])

def _nlines(path):
    """Line count, treating an absent generation as empty.

    Rotation renames a generation away; whether a given file exists at this
    instant depends on how many async rotations fired, which is not something
    this test gets to pin down.
    """
    try:
        with open(path) as fh:
            return len([1 for x in fh if x.strip()])
    except FileNotFoundError:
        return 0


active_lines = _nlines(mod.CHRONICLE_PATH)
rotated_lines = _nlines(rotated)
st, body = get("/chronicle?limit=500&q=endpoint%20entry")
check("GET /chronicle?q= draws matches from BOTH generations",
      st == 200 and body["count"] > active_lines,
      f"count={body.get('count')} active={active_lines} rotated={rotated_lines}")
# The real contract of perf/chronicle-archive: rotation LOSES NOTHING. Every
# line ever appended is still reachable through a q search, whichever
# generation (or the cold archive) it has since drifted into. The old form of
# this check compared against active+rotated only, which silently assumed
# exactly one rotation had fired -- it is a race, not an invariant, and it was
# the residual flake here after the rotate-thread wait went in.
check("GET /chronicle?q= reaches every retained line",
      body["count"] == TOTAL_APPENDED,
      f"count={body.get('count')} vs {TOTAL_APPENDED} appended "
      f"(active={active_lines} rotated={rotated_lines})")
# The cold archive (perf/chronicle-archive) flips rotation's old cost:
# a q search reaches the archive, so rotated-out entries are FOUND and the
# response says the deep walk happened.
st, body = get("/chronicle?limit=500&q=endpoint%20entry%203%20")
check("GET /chronicle?q= reaches rotated-out entries via the archive",
      st == 200 and body["count"] >= 1 and body.get("archive") is True,
      json.dumps(body)[:120])

st, body = get("/chronicle?kind=bogus")
check("GET /chronicle rejects a bad kind", st == 400, str(st))

# a respeak of an id living in the ROTATED generation
oldest = json.loads(open(rotated).readline())
before = svc._tts_queue.qsize()
recent_before = len(svc._queue_recent)
events_before = len(events)
st, body = post("/respeak", {"id": oldest["id"]})
check("POST /respeak finds a rotated id", st == 200
      and body.get("respeaking", {}).get("id") == oldest["id"],
      f"{st} {json.dumps(body)[:120]}")
# The dispatcher thread runs get() -> issue token -> claim-under-lock, so a
# single snapshot taken right after the POST can land between the dequeue
# and the claim and see the item NEITHER queued NOR current (measured: it
# did so 3/3 with speech-to-cli's #25 seam tree loaded, 0/3 without -- the
# import cost shifted the thread timing by ~1 ms, nothing else changed).
# Playback is asynchronous, so the assertion must be too: wait for any sign
# the item reached the speaker -- the fake tts started, the item is current
# or still queued, or it already finished into _queue_recent.
def _playback_seen():
    return (len(events) > events_before
            or svc._queue_current is not None
            or svc._tts_queue.qsize() > before
            or len(svc._queue_recent) > recent_before)
deadline = time.monotonic() + 2.0
while not _playback_seen() and time.monotonic() < deadline:
    time.sleep(0.01)
check("POST /respeak enqueued playback", _playback_seen(),
      f"events={events[-2:]} current={svc._queue_current is not None} "
      f"qsize={svc._tts_queue.qsize()} recent={list(svc._queue_recent)[-1:]}")
st, body = post("/respeak", {"id": 1})
check("POST /respeak unknown id -> 404", st == 404, str(st))
srv.shutdown()

# ------------------------------------------------------- D-Bus (async now)
handler = mod.DBusHandler(svc)
loop = GLib.MainLoop()
svc._main_loop = loop


class FakeInvocation:
    def __init__(self):
        self.done = threading.Event()
        self.payload = None
        self.error = None

    def return_value(self, variant):
        self.payload = variant
        self.done.set()

    def return_dbus_error(self, name, msg):
        self.error = (name, msg)
        self.done.set()


def dispatch(method, variant):
    inv = FakeInvocation()
    GLib.idle_add(lambda: (handler.handle_method_call(
        None, None, mod.OBJECT_PATH, mod.INTERFACE_NAME, method, variant, inv),
        False)[-1])
    t = threading.Thread(target=lambda: (inv.done.wait(15),
                                         GLib.idle_add(loop.quit)), daemon=True)
    t.start()
    GLib.timeout_add(16000, lambda: (loop.quit(), False)[-1])
    loop.run()
    return inv


inv = dispatch("GetChronicle", GLib.Variant("(i)", (12,)))
entries = json.loads(inv.payload.unpack()[0]) if inv.payload else []
check("D-Bus GetChronicle still replies", inv.error is None and len(entries) == 12,
      str(inv.error or len(entries)))
check("D-Bus GetChronicle oldest-first",
      [e["id"] for e in entries] == sorted(e["id"] for e in entries))

inv = dispatch("GetChronicle", GLib.Variant("(i)", (0,)))   # 0 -> default 20
entries = json.loads(inv.payload.unpack()[0]) if inv.payload else []
check("D-Bus GetChronicle(0) defaults to 20", len(entries) == 20, str(len(entries)))

inv = dispatch("Respeak", GLib.Variant("(x)", (oldest["id"],)))
check("D-Bus Respeak(rotated id) -> True",
      inv.payload is not None and inv.payload.unpack()[0] is True,
      str(inv.error or (inv.payload and inv.payload.unpack())))

inv = dispatch("Respeak", GLib.Variant("(x)", (1,)))
check("D-Bus Respeak(unknown) -> False",
      inv.payload is not None and inv.payload.unpack()[0] is False,
      str(inv.error))

inv = dispatch("Respeak", GLib.Variant("(x)", (0,)))   # 0 -> last spoken line
check("D-Bus Respeak(0) replays the last spoken line",
      inv.payload is not None and inv.payload.unpack()[0] is True,
      str(inv.error))

print("\nRESULT:", "ENDPOINTS OK" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
