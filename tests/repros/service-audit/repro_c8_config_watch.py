#!/usr/bin/env python3
"""C8: a prefs.js edit to config.json applies WITHOUT a hotkey press (#124).

Before: _reload_config_flags() had exactly one caller, start_listening() when
not quick. prefs.js promised "Most settings apply live", but a flip of
speech_backend (wyoming.prefer_local reads state.CONFIG, so agent POST /speak
and GET /status kept the old backend), wake_word (the watcher polls CONFIG) or
chronicle did nothing until the next NON-quick hotkey press -- and wake-opened
and loop sessions are quick=True, so they never reloaded at all.

  C8a  _start_config_watch exists and returns a GLib source id
  C8b  writing speech_backend/wake_word/chronicle to CONFIG_PATH flips CONFIG
       from the main loop alone -- start_listening is stubbed to FAIL if called
  C8c  the live consequence: wyoming.skip_reason() follows the flip
  C8d  an unchanged file is not re-parsed (the mtime gate still holds)

Exit 0 = all hold; 1 = a verdict failed; 2 = setup failure.
"""
import builtins
import json
import os
import sys
import time

import harness

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} ({label}) {detail}")
    if not cond:
        FAILS.append(label)


def main():
    mod, _events = harness.load(fake_tts_seconds=0.1)
    GLib = mod.GLib
    svc = harness.make_service(mod)

    # The bug is that reload rides on this call. It must not be needed.
    def _no_hotkey(*a, **k):
        raise AssertionError("start_listening() was called -- reload must not depend on it")
    svc.start_listening = _no_hotkey

    # Prefs-shaped starting point: all three flags in their pinned state.
    if (mod.CONFIG.get("speech_backend", "azure") == "local"
            or mod.CONFIG.get("wake_word") is not False
            or mod.CONFIG.get("chronicle") is not False):
        print("!! SETUP FAILURE: CONFIG pins did not take")
        return 2
    wy = mod.wyoming_mod
    mod.CONFIG["wyoming_host"] = "127.0.0.1"   # prefer_local needs a target
    wy.mark_azure_up(); wy.mark_local_up()

    start = getattr(svc, "_start_config_watch", None)
    if start is None:
        check("C8a", False, "GnomeSpeaksService has no _start_config_watch")
        print("FAIL: 1 verdict(s) -- config reload is still a side effect of the hotkey")
        return 1
    source_id = start()
    check("C8a", isinstance(source_id, int) and source_id > 0, f"source id {source_id}")

    # What prefs.js does: merge-on-write of the same file, published by rename.
    with open(mod.CONFIG_PATH) as f:
        disk = json.load(f)
    disk.update({"speech_backend": "local", "wake_word": True, "chronicle": True})
    tmp = mod.CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(disk, f)
    os.replace(tmp, mod.CONFIG_PATH)
    # Bump mtime past any filesystem granularity so the gate cannot mask the write.
    st = os.stat(mod.CONFIG_PATH)
    os.utime(mod.CONFIG_PATH, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))

    # Spin the main loop only -- no D-Bus, no hotkey, no start_listening.
    loop = GLib.MainLoop()
    interval = getattr(svc, "CONFIG_WATCH_SECONDS", 2)
    deadline = time.monotonic() + interval * 3 + 1

    def _poll():
        flipped = (mod.CONFIG.get("speech_backend") == "local"
                   and mod.CONFIG.get("wake_word") is True
                   and mod.CONFIG.get("chronicle") is True)
        if flipped or time.monotonic() > deadline:
            loop.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE
    GLib.timeout_add(50, _poll)
    t0 = time.monotonic()
    loop.run()
    took = time.monotonic() - t0
    check("C8b", mod.CONFIG.get("speech_backend") == "local"
          and mod.CONFIG.get("wake_word") is True
          and mod.CONFIG.get("chronicle") is True,
          f"after {took:.1f}s: speech_backend={mod.CONFIG.get('speech_backend')!r} "
          f"wake_word={mod.CONFIG.get('wake_word')!r} chronicle={mod.CONFIG.get('chronicle')!r}")
    check("C8c", wy.skip_azure() and wy.skip_reason() == "prefer_local",
          f"skip_azure={wy.skip_azure()} reason={wy.skip_reason()}")

    # Mtime gate: an unchanged file must not be parsed again.
    cached = svc._config_mtime
    opened = []
    orig = builtins.open

    def spy(path, *a, **k):
        if path == mod.CONFIG_PATH:
            opened.append(path)
        return orig(path, *a, **k)
    builtins.open = spy
    try:
        svc._reload_config_flags()
    finally:
        builtins.open = orig
    check("C8d", not opened and svc._config_mtime == cached,
          f"re-opens of an unchanged config: {len(opened)}")

    GLib.source_remove(source_id)
    # The watcher thread reads CONFIG['wake_word'] every 0.5 s; put it back
    # before exit so it never tries to open a mic from a test.
    mod.CONFIG["wake_word"] = False

    if FAILS:
        print(f"FAIL: {len(FAILS)} verdict(s) -- config reload is still a side effect of the hotkey")
        return 1
    print("PASS: prefs edits to config.json apply from the main loop without a hotkey press")
    return 0


if __name__ == "__main__":
    sys.exit(main())
