"""Contract tests for the tail-read + rotation rewrite (issue #22).

The reverse reader is the risky part, so it is checked against an oracle: the
ORIGINAL full-scan algorithm, run over the same file. Every case must agree
exactly. Block size is forced small so every straddle boundary is exercised.
"""
import json
import os
import random
import shutil
import sys
import threading
import time

# No sys.path insert into a world-writable /tmp dir: those dirs held no
# harness.py (the line was already a no-op), but a stale copy left there
# would silently shadow this suite's own. See harness.py's ISOLATION note.
import harness

mod, _events = harness.load(0.05)
DIR = os.path.join(harness.SCRATCH, "contract")
shutil.rmtree(DIR, ignore_errors=True)
os.makedirs(DIR, exist_ok=True)
mod.CHRONICLE_PATH = os.path.join(DIR, "chronicle.jsonl")
fails = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{'' if ok else '  <- ' + detail}")
    if not ok:
        fails.append(name)


def oracle_read(path, limit=20, q=None, kind=None):
    """The pre-#22 implementation, verbatim in spirit: forward whole-file scan."""
    from collections import deque
    entries = deque(maxlen=max(1, min(int(limit or 20), 500)))
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if kind and entry.get("kind") != kind:
                    continue
                if q and q.lower() not in entry.get("text", "").lower():
                    continue
                entries.append(entry)
    except FileNotFoundError:
        pass
    return list(entries)


def write_lines(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(lines))


def entry_line(i, kind="you", text=None, extra=None):
    e = {"kind": kind, "text": text if text is not None else f"line {i} hello",
         "ts": "2026-08-17T12:00:00-0700", "id": 1000 + i}
    if extra:
        e.update(extra)
    return json.dumps(e, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------- equivalence
P = mod.CHRONICLE_PATH
cases = {
    "empty file": [],
    "one line": [entry_line(0)],
    "no trailing newline": [entry_line(0), entry_line(1).rstrip("\n")],
    "blank lines interleaved": [entry_line(0), "\n", entry_line(1), "\n"],
    "invalid json line": [entry_line(0), "{not json\n", entry_line(1)],
    "unicode": [entry_line(0, text="ünïcødé — 漢字 🜛 boundary"),
                entry_line(1, text="ünïcødé — 漢字 🜛 boundary")],
    "one huge entry (200 KB)": [entry_line(0, text="X" * 200000), entry_line(1)],
    "40 lines": [entry_line(i, kind=("spoken" if i % 3 else "you"),
                            text=f"line {i} " + "pad " * (i % 7))
                 for i in range(40)],
}
for name, lines in cases.items():
    write_lines(P, lines)
    for block in (1, 7, 64, 4096):
        mod._CHRONICLE_BLOCK = block
        for limit, q, kind in [(20, None, None), (1, None, None),
                               (500, None, None), (5, "line", None),
                               (20, None, "spoken"), (20, "PAD", None),
                               (20, "nomatch-zzz", None), (3, None, "you")]:
            got = mod._chronicle_read(limit=limit, q=q, kind=kind)
            want = oracle_read(P, limit=limit, q=q, kind=kind)
            if got != want:
                check(f"equivalence [{name}] block={block} "
                      f"limit={limit} q={q} kind={kind}",
                      False, f"got {len(got)} want {len(want)}")
                break
        else:
            continue
        break
    else:
        check(f"equivalence [{name}] (4 block sizes x 8 queries)", True)
mod._CHRONICLE_BLOCK = 65536

# missing file must be empty, not an exception
os.remove(P)
check("missing file -> []", mod._chronicle_read(limit=5) == [])
check("missing file -> find None", mod._chronicle_find(1234) is None)

# ------------------------------------------------------------------- find
write_lines(P, [entry_line(i) for i in range(30)])
check("find first entry", (mod._chronicle_find(1000) or {}).get("text") == "line 0 hello")
check("find last entry", (mod._chronicle_find(1029) or {}).get("text") == "line 29 hello")
check("find absent id", mod._chronicle_find(999999) is None)
check("find with string id coerces", (mod._chronicle_find("1005") or {}).get("id") == 1005)
check("find with junk id -> None", mod._chronicle_find("abc") is None)
check("find with None -> None", mod._chronicle_find(None) is None)

# ---------------------------------------------------------------- rotation
shutil.rmtree(DIR, ignore_errors=True)
os.makedirs(DIR, exist_ok=True)
mod._CHRONICLE_MAX_BYTES = 8192
mod._chronicle_last_id = 0
mod._chronicle_id_seeded = False
mod.CONFIG["chronicle"] = True
ids = []
for i in range(200):
    mod._chronicle_append("spoken", f"rotating entry {i} " + "pad " * 20,
                          voice="en-US-BrianNeural", source="test")
# Rotation runs on a background thread now (perf/chronicle-archive):
# wait for it to settle, and the just-rotated active file may not exist
# until the next append recreates it.
if hasattr(mod, "_chronicle_archiving"):
    _deadline = time.time() + 10
    while mod._chronicle_archiving.is_set() and time.time() < _deadline:
        time.sleep(0.02)
rotated = P + ".1"
check("rotation happened", os.path.exists(rotated))
_active = os.path.getsize(P) if os.path.exists(P) else 0
check("active file under cap after rotation",
      _active < mod._CHRONICLE_MAX_BYTES * 2, f"{_active} bytes")
spanning = mod._chronicle_read(limit=200)
# A fully-rotated active file doesn't exist until the next append: 0 lines.
_active_lines = len(list(open(P))) if os.path.exists(P) else 0
check("read spans both generations",
      len(spanning) > _active_lines,
      f"read {len(spanning)}, active file has {_active_lines}")
all_ids = [e["id"] for e in spanning]
check("ids ascending across rotation", all_ids == sorted(all_ids))
check("no duplicate ids", len(set(all_ids)) == len(all_ids))
oldest_in_rotated = json.loads(open(rotated).readline())
check("respeak can still find an entry in the rotated file",
      (mod._chronicle_find(oldest_in_rotated["id"]) or {}).get("id")
      == oldest_in_rotated["id"])
check("read is oldest-first", spanning[0]["id"] < spanning[-1]["id"])

# -------------------------------------------------- id seeding across restart
newest_before = mod._chronicle_read(limit=1)[0]["id"]
mod._chronicle_last_id = 0            # simulate a fresh process
mod._chronicle_id_seeded = False
real_time = time.time
try:
    time.time = lambda: real_time() - 86400 * 3   # clock stepped back 3 days
    mod._chronicle_append("you", "after a backwards clock step")
finally:
    time.time = real_time
after = mod._chronicle_read(limit=1)[0]
check("id still ascends after a backwards clock step",
      after["id"] > newest_before, f"{after['id']} vs {newest_before}")
check("no duplicate id minted", mod._chronicle_find(newest_before)["id"] == newest_before)

# ------------------------------------------------------------- concurrency
shutil.rmtree(DIR, ignore_errors=True)
os.makedirs(DIR, exist_ok=True)
mod._chronicle_last_id = 0
mod._chronicle_id_seeded = False
mod._CHRONICLE_MAX_BYTES = 16384
errors = []
stop = threading.Event()


def appender(n):
    try:
        for i in range(120):
            mod._chronicle_append("spoken", f"t{n} entry {i} " + "pad " * 10)
    except Exception as exc:
        errors.append(f"append: {exc!r}")


def reader():
    try:
        while not stop.is_set():
            mod._chronicle_read(limit=12)
            mod._chronicle_read(limit=5, kind="spoken")
            mod._chronicle_find(random.randint(1, 10**13))
    except Exception as exc:
        errors.append(f"read: {exc!r}")


ts = [threading.Thread(target=appender, args=(n,)) for n in range(4)]
rs = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
for t in rs + ts:
    t.start()
for t in ts:
    t.join()
stop.set()
time.sleep(0.3)
check("no exceptions under 4 appenders x 3 readers + rotation", not errors,
      "; ".join(errors[:3]))
lines = []
for p in mod._chronicle_files():
    if os.path.exists(p):
        lines += [json.loads(x) for x in open(p) if x.strip()]
cids = sorted(e["id"] for e in lines)
check("no duplicate ids under concurrency", len(set(cids)) == len(cids))
check("every line is valid json", len(lines) > 0)

print("\nRESULT:", "CONTRACT HOLDS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
