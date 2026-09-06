"""C2: _user_speech_active is a boolean Event shared by N user-speech paths.

Schedule (deterministic, no tight race needed):
  1. speak("USERONE")           -> hold set, worker A plays
  2. POST /speak equivalent     -> agent item queued, dispatcher correctly HELD
  3. speak("USERTWO")           -> sets hold (already set), then stop() joins A;
                                   A's finally clears the hold; B starts with
                                   the hold CLEAR.
  => the dispatcher escapes and plays AGENT on top of the user's USERTWO.
"""
import sys
import time

# No sys.path insert into a world-writable /tmp dir: those dirs held no
# harness.py (the line was already a no-op), but a stale copy left there
# would silently shadow this suite's own. See harness.py's ISOLATION note.
import harness

mod, events = harness.load(fake_tts_seconds=1.2)
svc = harness.make_service(mod)
time.sleep(0.3)  # let the dispatcher thread reach its gate loop

svc.speak("USERONE clipboard reading")
time.sleep(0.2)
svc.enqueue_speech("AGENT status line", source="agent-x")
time.sleep(0.2)
print("hold set while USERONE plays:", svc._user_speech_active.is_set())

svc.speak("USERTWO selection reading")
print("hold set while USERTWO plays:", svc._user_speech_active.is_set())
time.sleep(2.5)

sp = harness.spans(events)
print("\nplayback spans (seconds since start):")
for tag, (s, e) in sorted(sp.items(), key=lambda kv: kv[1][0]):
    print(f"  {tag:9s} {s:.2f} -> {e if e is None else round(e, 2)}")

ov = 0.0
if "AGENT" in sp and "USERTWO" in sp:
    ov = harness.overlap(sp["AGENT"], sp["USERTWO"])
print(f"\nAGENT/USERTWO overlap: {ov:.2f}s")
print("RESULT:", "BUG REPRODUCED (agent speech over user speech)" if ov > 0.05
      else "no overlap")
sys.exit(1 if ov > 0.05 else 0)
