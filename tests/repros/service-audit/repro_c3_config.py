"""C3: _save_config_flag is an unlocked, non-atomic read-modify-write.

Two distinct failures, both against the REAL function (the config path is
redirected to /tmp for the duration of the test):

  (a) lost update: two threads (D-Bus pool + HTTP spell thread) each
      read-modify-write the whole file, so one flag silently vanishes.
  (b) truncation window: open(path, "w") empties the file in place. Any
      concurrent reader sees a partial document. prefs.js's merge-on-write
      does exactly this read, and on a parse failure falls back to
      `onDisk = {}` -> it then writes back a config.json containing ONLY the
      key it was editing, destroying the Azure keys and every other setting.
"""
import json
import os
import sys
import threading
import time

# No sys.path insert into a world-writable /tmp dir: those dirs held no
# harness.py (the line was already a no-op), but a stale copy left there
# would silently shadow this suite's own. See harness.py's ISOLATION note.
import harness

TMP = os.path.join(harness.SCRATCH, "c3-config.json")
mod, _events = harness.load(fake_tts_seconds=0.1)

# Redirect only the config path (this process only).
CONFIG_PATH_STR = "~/.config/speech-to-cli/config.json"
if hasattr(mod, "CONFIG_PATH"):
    mod.CONFIG_PATH = TMP           # post-fix: module constant
else:
    _real = os.path.expanduser      # pre-fix: the path is inlined twice
    os.path.expanduser = lambda p: TMP if p == CONFIG_PATH_STR else _real(p)

svc = harness.make_service(mod)


def seed():
    with open(TMP, "w") as f:
        json.dump({"key": "AZURE-SECRET", "region": "eastus",
                   "voice": "en-US-BrianNeural"}, f, indent=2)


# --- (a) lost update ------------------------------------------------------
seed()
N = 8
barrier = threading.Barrier(N)


def writer(i):
    barrier.wait()
    svc._save_config_flag(f"flag{i}", i)


threads = [threading.Thread(target=writer, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()
with open(TMP) as f:
    disk = json.load(f)
present = [k for k in (f"flag{i}" for i in range(N)) if k in disk]
lost = N - len(present)
print(f"(a) lost update: {len(present)}/{N} flags survived "
      f"({lost} silently lost); azure key present={'key' in disk}")

# --- (b) truncation window ------------------------------------------------
seed()
stop = threading.Event()
bad = {"parse_error": 0, "empty": 0, "reads": 0}


def prefs_like_reader():
    """Exactly what prefs.js _saveConfig does before merging."""
    while not stop.is_set():
        bad["reads"] += 1
        try:
            with open(TMP, "rb") as f:
                raw = f.read()
            if not raw:
                bad["empty"] += 1
                continue
            json.loads(raw)
        except FileNotFoundError:
            bad["empty"] += 1
        except (json.JSONDecodeError, ValueError):
            bad["parse_error"] += 1


r = threading.Thread(target=prefs_like_reader, daemon=True)
r.start()
for i in range(400):
    svc._save_config_flag("chronicle", bool(i % 2))
stop.set()
r.join(timeout=2)
partial = bad["parse_error"] + bad["empty"]
print(f"(b) truncation window: {partial} of {bad['reads']} concurrent reads "
      f"saw a partial/empty config "
      f"(parse_error={bad['parse_error']}, empty={bad['empty']})")
print("    -> each one is a prefs.js save that would rewrite config.json "
      "from {} and drop the Azure keys")

print("\nRESULT:", "BUG REPRODUCED" if (lost or partial) else "clean")
sys.exit(1 if (lost or partial) else 0)
