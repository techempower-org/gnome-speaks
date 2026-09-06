"""Archive rotation verification: no line ever lost, no id ever duplicated.

Forces many rotations with a tiny cap, then checks the promises the
chronicle-archive branch makes:
  1. NO LOSS      — union(hot + archive) ids == every id ever appended
  2. NO DUPES     — a full include_archive read returns each id once
  3. BOUNDED HOT  — the recent list (no q) never reads the archive
  4. DEEP FIND    — _chronicle_find reaches an id rotated into the archive
  5. CLOCK STEP   — ids stay unique/ascending across a backwards clock step,
                    seeded from the archive even if hot files are cleared
Run with GS_SVC_PATH pointing at the branch under test.
"""
import json
import os
import shutil
import sys
import time

# Default to the main checkout, not a worktree that no longer exists.
# Repo-relative by construction: this file lives at
# tests/repros/<suite>/<file>.py, so four dirnames reach the repo root. #59:
# the old defaults were absolute paths into ~/Projects/gnome-speaks-wt/<name>/
# worktrees that no longer existed, so a bare run died with FileNotFoundError
# instead of testing anything.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SVC_DEFAULT = os.path.join(REPO_ROOT, "gnome-speaks-service.py")
# Scratch lives in the repo's gitignored tmp/, keyed by PID. Never a fixed
# shared path: two agents running a suite at once used to corrupt each other
# and it read exactly like a service regression.
SCRATCH_ROOT = os.path.join(REPO_ROOT, "tmp", "repros")

os.environ.setdefault(
    "GS_SVC_PATH",
    SVC_DEFAULT)
os.environ["GNOME_SPEAKS_CHRONICLE_MAX_BYTES"] = "20000"  # ~50 entries/gen

import harness  # noqa: E402  (sets XDG_STATE_HOME; we point it somewhere fresh)

# Was "/tmp/audit-repro/archive-state" -- the CHRONICLE suite writing into
# the AUDIT suite's directory, colliding even with one agent running.
STATE_DIR = os.path.join(harness.SCRATCH, "archive-state")
shutil.rmtree(STATE_DIR, ignore_errors=True)
os.environ["XDG_STATE_HOME"] = STATE_DIR
os.makedirs(STATE_DIR, exist_ok=True)

svc, _events = harness.load()

FAIL = 0


def check(name, ok, detail=""):
    global FAIL
    print(("  ok   " if ok else "  FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        FAIL += 1


def settle():
    """Wait out any in-flight background rotation."""
    deadline = time.time() + 10
    while svc._chronicle_archiving.is_set() and time.time() < deadline:
        time.sleep(0.02)
    assert not svc._chronicle_archiving.is_set(), "rotation never settled"


def read_ids(path):
    ids = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ids.append(json.loads(line)["id"])
    except FileNotFoundError:
        pass
    return ids


# -- 1+2: append enough padded entries to force several rotations ----------
appended = []
for i in range(300):
    svc._chronicle_append("you" if i % 3 else "spoken",
                          f"line {i:04d} " + "x" * 300)
    if i % 25 == 0:
        settle()
settle()

hot_ids = []
for p in svc._chronicle_files():
    hot_ids += read_ids(p)
archive_ids = read_ids(svc._chronicle_archive_path())

all_read = svc._chronicle_read(limit=100000, q="line", include_archive=True)
union = set(hot_ids) | set(archive_ids)

check("archive exists and holds rotated history", len(archive_ids) > 0,
      f"{len(archive_ids)} archived, {len(hot_ids)} hot")
check("no loss: hot+archive covers all 300 appends", len(union) == 300,
      f"union={len(union)}")
check("no dupes in full include_archive read",
      len(all_read) == len({e['id'] for e in all_read}) == 300,
      f"read {len(all_read)}")

# -- 3: recent list must stay a bounded hot read ----------------------------
recent = svc._chronicle_read(limit=100000)
recent_ids = {e["id"] for e in recent}
check("recent list never reaches the archive",
      recent_ids.isdisjoint(set(archive_ids) - set(hot_ids)),
      f"recent={len(recent)}")

# -- 4: find falls through to the archive -----------------------------------
oldest_id = min(union)
in_archive_only = oldest_id in set(archive_ids) and oldest_id not in set(hot_ids)
found = svc._chronicle_find(oldest_id)
check("oldest id lives only in the archive (test is meaningful)",
      in_archive_only)
check("_chronicle_find reaches an archived id",
      found is not None and found["id"] == oldest_id)

# -- 5: backwards clock step + hot wipe: ids seeded from the archive --------
real_time = time.time
try:
    time.time = lambda: real_time() - 3 * 86400  # 3-day backwards step
    before = svc._chronicle_last_id
    svc._chronicle_append("you", "clock step entry")
    check("backwards clock still mints a fresh ascending id",
          svc._chronicle_last_id > before)
finally:
    time.time = real_time

settle()
for p in svc._chronicle_files():
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass
svc._chronicle_id_seeded = False
svc._chronicle_last_id = 0
try:
    time.time = lambda: real_time() - 3 * 86400
    svc._chronicle_append("you", "post-wipe entry")
    top_archive = max(read_ids(svc._chronicle_archive_path()))
    check("id seeding falls through to the archive after a hot wipe",
          svc._chronicle_last_id > top_archive,
          f"minted {svc._chronicle_last_id} > archive max {top_archive}")
finally:
    time.time = real_time

print()
print("RESULT: " + ("ARCHIVE CONTRACT HOLDS" if FAIL == 0 else f"{FAIL} FAILURES"))
sys.exit(1 if FAIL else 0)
