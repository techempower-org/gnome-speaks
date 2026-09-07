#!/usr/bin/env python3
"""repro: speech_backend=local prefers the LAN Wyoming server with Azure as the
fallback, and the service reports WHY Azure is skipped (#104, #103).

Before: the only way to prefer local was SPEECH_FORCE_OFFLINE=1 (a drop-in),
which also had NO Azure fallback, and the log said "Azure marked down" for it.

  B1  default (azure): Azure live, skip_reason None, route.backend "azure"
  B2  speech_backend=local + wyoming configured: skip_azure True, reason prefer_local
  B3  local failure (mark_local_down): skip_azure False -> Azure is the fallback
  B4  local cooldown elapsed: back to local
  B5  forced offline: reason "forced", azure_fallback_allowed False (never Azure)
  B6  the log words never say "marked down" for prefer_local / forced
  B7  speech_backend is in _SYNC_FLAGS (a prefs change hot-reloads)

Needs the speech-to-cli that carries wyoming.prefer_local (SPEECH_ENGINE_PATH).
Run:  GS_SVC_PATH=<gnome-speaks-service.py> python3 repro_c6_speech_backend.py
Exit 0 = all hold; 1 = a verdict failed; 2 = setup failure.
"""
import atexit
import importlib.util
import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SVC_PATH = os.environ.get("GS_SVC_PATH", os.path.join(REPO_ROOT, "gnome-speaks-service.py"))
SCRATCH_ROOT = os.path.join(REPO_ROOT, "tmp", "repros")
_STATE = os.path.join(SCRATCH_ROOT, f"service-audit-backend-{os.getpid()}", "state")
os.makedirs(_STATE, exist_ok=True)
atexit.register(shutil.rmtree, os.path.dirname(_STATE), True)
os.environ["XDG_STATE_HOME"] = _STATE
os.environ.setdefault("SPEECH_ENGINE_PATH", os.path.expanduser("~/Projects/speech-to-cli"))
os.environ.pop("SPEECH_FORCE_OFFLINE", None)
FAILS = []


def check(label, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} ({label}) {detail}")
    if not cond:
        FAILS.append(label)


def load():
    sys.path.insert(0, os.path.dirname(SVC_PATH))
    spec = importlib.util.spec_from_file_location("gsvc_backend", SVC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gsvc_backend"] = mod
    spec.loader.exec_module(mod)
    mod.CONFIG_PATH = os.path.join(_STATE, "config.json")
    return mod


def main():
    if not os.path.isfile(SVC_PATH):
        print(f"!! SETUP FAILURE: no such file: {SVC_PATH}")
        return 2
    mod = load()
    wy = mod.wyoming_mod
    if not hasattr(wy, "prefer_local"):
        # Old speech-to-cli: the feature cannot exist. That IS the pre-fix state,
        # so it is a verdict, not a setup failure -- but say which half is missing.
        print("  FAIL (B2) speech-to-cli has no wyoming.prefer_local -- speech_backend unsupported")
        print("FAIL: 1 verdict(s) -- offline-first is not available")
        return 1
    # wyoming reads state.CONFIG; the service's CONFIG is that same dict
    cfg = wy.state.CONFIG
    cfg["wyoming_host"] = "127.0.0.1"
    wy.mark_azure_up(); wy.mark_local_up()
    clock = {"t": 1000.0}
    real_time = wy.time.time
    wy.time.time = lambda: clock["t"]
    try:
        cfg.pop("speech_backend", None)
        route = mod.speech_route()
        check("B1", not wy.skip_azure() and wy.skip_reason() is None and route["backend"] == "azure",
              f"default: skip={wy.skip_azure()} reason={wy.skip_reason()} route={route['backend']}")

        cfg["speech_backend"] = "local"
        check("B2", wy.skip_azure() and wy.skip_reason() == "prefer_local" and mod.speech_route()["offline_reason"] == "prefer_local",
              f"local: skip={wy.skip_azure()} reason={wy.skip_reason()}")

        wy.mark_local_down(cooldown=60)
        check("B3", not wy.skip_azure() and wy.skip_reason() is None and wy.azure_fallback_allowed() and mod.speech_route()["local_down"],
              f"after local failure: skip={wy.skip_azure()} reason={wy.skip_reason()} local_down={wy.local_down()}")

        clock["t"] += 61
        check("B4", wy.skip_azure() and wy.skip_reason() == "prefer_local",
              f"61 s later: skip={wy.skip_azure()} reason={wy.skip_reason()}")

        os.environ["SPEECH_FORCE_OFFLINE"] = "1"
        check("B5", wy.skip_azure() and wy.skip_reason() == "forced" and not wy.azure_fallback_allowed(),
              f"forced: reason={wy.skip_reason()} fallback_allowed={wy.azure_fallback_allowed()}")
        forced_words = mod._speech_route_words()
        os.environ.pop("SPEECH_FORCE_OFFLINE", None)
        local_words = mod._speech_route_words()
        check("B6", "marked down" not in forced_words.lower() and "marked down" not in local_words.lower()
              and "forced" in forced_words.lower() and "local" in local_words.lower(),
              f"words: forced={forced_words!r} local={local_words!r}")

        flags = getattr(getattr(mod, "GnomeSpeaksService", None), "_SYNC_FLAGS", ())
        check("B7", "speech_backend" in flags, f"GnomeSpeaksService._SYNC_FLAGS has it: {'speech_backend' in flags}")
    finally:
        wy.time.time = real_time
        wy.mark_local_up()

    if FAILS:
        print(f"FAIL: {len(FAILS)} verdict(s) -- offline-first routing / reporting is wrong")
        return 1
    print("PASS: speech_backend=local prefers Wyoming, falls back to Azure on local failure, and reports why")
    return 0


if __name__ == "__main__":
    sys.exit(main())
