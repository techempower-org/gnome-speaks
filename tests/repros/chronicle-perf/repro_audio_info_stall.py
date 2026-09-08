"""Issue #135 repro: GetAudioInfo runs the PipeWire probes ON the GLib main loop.

Same instrument as repro_chronicle_stall.py, pointed at the other inline
method: a 20ms heartbeat runs on a real GLib main loop while
DBusHandler.handle_method_call("GetAudioInfo") is dispatched exactly as GDBus
dispatches it. get_audio_info() calls _refresh_audio_detection() (wpctl status,
3 s timeout; pw-dump when the sink changed) and has_echo_cancel() (pw-cli
list-objects, 3 s, until cached). Here both are stubbed: the refresh sleeps
300 ms standing in for a slow probe, the EC check returns at once -- so the
measurement is the dispatch path, not PipeWire.

The extension issues GetAudioInfo at proxy init and on every bus-name-appeared,
i.e. at login and after every service restart -- the window where #50 measured
these probes at 3.9 s. For as long as the call is answered inline the service
emits no StateChanged/AudioLevel/SubtitleUpdate and StartListening/Stop wait
behind it.

Hedge, verbatim from the issue: the inline cost at login is INFERRED from
#50's measurement, not re-measured; on warm PipeWire wpctl is ~12 ms and the
stall is invisible. This repro therefore asserts on the dispatch shape (does
a slow probe stall the loop?), not on a live PipeWire timing.
"""
import json
import statistics
import sys
import threading
import time

import harness

PROBE_SECONDS = 0.3

mod, _events = harness.load(0.05)
from gi.repository import GLib  # noqa: E402

svc = harness.make_service(mod)
handler = mod.DBusHandler(svc)

# harness.load() already stubs _refresh_audio_detection to a no-op; replace it
# with a slow one, and pin the EC probe so no pw-cli is spawned either.
probe_thread = []


def slow_refresh(*a, **k):
    probe_thread.append(threading.current_thread().name)
    time.sleep(PROBE_SECONDS)


mod._refresh_audio_detection = slow_refresh
mod.has_echo_cancel = lambda *a, **k: False


class FakeInvocation:
    """Stands in for Gio.DBusMethodInvocation."""

    def __init__(self):
        self.done = threading.Event()
        self.error = None
        self.payload = None

    def return_value(self, variant):
        self.payload = variant
        self.done.set()

    def return_dbus_error(self, name, msg):
        self.error = (name, msg)
        self.done.set()


loop = GLib.MainLoop()
svc._main_loop = loop
beats = []


def heartbeat():
    beats.append(time.monotonic())
    return True


GLib.timeout_add(20, heartbeat)
inv = FakeInvocation()
main_thread = threading.current_thread().name


def fire():
    # Exactly how GDBus dispatches an incoming call: on the main loop.
    handler.handle_method_call(None, None, mod.OBJECT_PATH, mod.INTERFACE_NAME,
                              "GetAudioInfo", GLib.Variant("()", ()), inv)
    return False


def finish():
    if inv.done.wait(timeout=5):
        GLib.idle_add(loop.quit)
    return False


GLib.timeout_add(300, fire)
threading.Thread(target=finish, daemon=True).start()
GLib.timeout_add(6000, lambda: (loop.quit(), False)[-1])
loop.run()

gaps = [round((b - a) * 1000, 1) for a, b in zip(beats, beats[1:])]
stall = max(gaps) if gaps else 0.0
answered = inv.done.is_set()
info = json.loads(inv.payload.unpack()[0]) if answered and inv.error is None else None
on_main = bool(probe_thread) and probe_thread[0] == main_thread

print(f"probe: {PROBE_SECONDS * 1000:.0f} ms stand-in, ran on "
      f"{probe_thread[0] if probe_thread else 'NO THREAD'} "
      f"(main is {main_thread})")
print(f"main-loop stall: worst heartbeat gap {stall:.1f} ms "
      f"(median {statistics.median(gaps):.1f} ms over {len(gaps)} beats)")
print(f"GetAudioInfo answered={answered} error={inv.error} "
      f"keys={sorted(info) if info else None}")

if not answered or info is None:
    print("\n!! SETUP FAILURE: GetAudioInfo never returned a payload")
    sys.exit(2)
if set(info) != {"device_type", "echo_cancel", "half_duplex", "description"}:
    print("\n!! SETUP FAILURE: unexpected GetAudioInfo payload shape")
    sys.exit(2)

# The stall threshold is well under the probe: a 300 ms probe run inline
# produces a >=300 ms gap, run in the pool it produces ~20 ms beats.
verdict = stall > 100 or on_main
print("\nRESULT:", "SLOW (issue #135 reproduced)" if verdict else "FAST")
sys.exit(1 if verdict else 0)
