#!/usr/bin/env python3
"""repro: a stop while the batch (VAD) recorder is running FINISHES it early
and keeps the frames (#110 -- "the badge was stuck in listening mode").

stop_listening() sets the service's _stop_event and deliberately not the
cancel wire; the VAD recorder used to watch only the wire, so in offline /
loop mode a tap did nothing until VAD silence or 30 s.

  V1  speech-to-cli stt() accepts stop_when (the service detects it)
  V2  record_with_vad(stop_when) returns EARLY with the frames captured so far
      (fake recorder that never goes silent)
  V3  without stop_when the same recorder runs to max_seconds (control: the
      predicate is what ends it, not the fake)
  V4  the service passes stop_when=_stop_event.is_set to stt_dispatch

Needs the speech-to-cli that carries stop_when (SPEECH_ENGINE_PATH).
Run:  GS_SVC_PATH=<gnome-speaks-service.py> python3 repro_c7_loop_tap_stops_vad.py
Exit 0 = all hold; 1 = a verdict failed; 2 = setup failure.
"""
import atexit
import importlib
import importlib.util
import inspect
import io
import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SVC_PATH = os.environ.get("GS_SVC_PATH", os.path.join(REPO_ROOT, "gnome-speaks-service.py"))
SCRATCH_ROOT = os.path.join(REPO_ROOT, "tmp", "repros")
_STATE = os.path.join(SCRATCH_ROOT, f"service-audit-looptap-{os.getpid()}", "state")
os.makedirs(_STATE, exist_ok=True)
atexit.register(shutil.rmtree, os.path.dirname(_STATE), True)
os.environ["XDG_STATE_HOME"] = _STATE
os.environ.setdefault("SPEECH_ENGINE_PATH", os.path.expanduser("~/Projects/speech-to-cli"))
FAILS = []


def check(label, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} ({label}) {detail}")
    if not cond:
        FAILS.append(label)


class FakeRecorder:
    """A recorder whose stdout yields loud frames forever -- VAD never sees silence."""
    def __init__(self, frame_bytes):
        self.reads = 0
        self._fb = frame_bytes
        self.stdout = self

    def read(self, n):
        self.reads += 1
        # alternating high-amplitude samples: energy well above any threshold
        return (b"\x00\x40\x00\xc0" * (self._fb // 4))[:n]


def main():
    sys.path.insert(0, os.path.dirname(SVC_PATH))
    spec = importlib.util.spec_from_file_location("gsvc_looptap", SVC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gsvc_looptap"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        print(f"!! SETUP FAILURE: cannot load service: {exc}")
        return 2
    mod.CONFIG_PATH = os.path.join(_STATE, "config.json")
    stt = importlib.import_module("stt")
    audio = importlib.import_module("audio")
    state = importlib.import_module("state")

    has = "stop_when" in inspect.signature(stt.stt).parameters
    check("V1", has and getattr(mod, "_STT_HAS_STOP_WHEN", False) == has,
          f"stt(stop_when) supported={has}; service detected={getattr(mod, '_STT_HAS_STOP_WHEN', None)}")
    if not has:
        print("FAIL: 1 verdict(s) -- speech-to-cli has no stop_when; a stop cannot end a VAD recording")
        return 1

    # Make the recorder deterministic and fast: no real VAD, tiny frames.
    state.HAS_VAD = False
    state.FRAME_BYTES = 64
    state.FRAME_MS = 20
    real_cal = audio.calibrate_noise
    audio.calibrate_noise = lambda proc: (1.0, [])
    try:
        rec = FakeRecorder(state.FRAME_BYTES)
        stop_at = {"n": 5}
        frames, _ = audio.record_with_vad(rec, max_seconds=30, stop_when=lambda: rec.reads >= stop_at["n"])
        check("V2", 3 <= len(frames) <= 6, f"stopped early with {len(frames)} frames after {rec.reads} reads")

        rec2 = FakeRecorder(state.FRAME_BYTES)
        frames2, _ = audio.record_with_vad(rec2, max_seconds=2)   # 2 s / 20 ms = 100 frames
        check("V3", len(frames2) >= 90, f"no predicate: ran to max_seconds -> {len(frames2)} frames")
    finally:
        audio.calibrate_noise = real_cal

    # V4: the service passes its stop event through
    seen = {}
    def fake_dispatch(**kw):
        seen.update(kw)
        return {"text": ""}
    mod.stt_dispatch = fake_dispatch
    src = inspect.getsource(mod).count("stop_when=self._stop_event.is_set")
    check("V4", src == 1, f"service passes stop_when=self._stop_event.is_set at {src} site(s)")

    if FAILS:
        print(f"FAIL: {len(FAILS)} verdict(s) -- a stop does not end a running VAD recording")
        return 1
    print("PASS: a stop finishes the VAD recording early and keeps the frames; the service wires it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
