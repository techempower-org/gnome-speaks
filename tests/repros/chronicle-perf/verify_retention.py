"""Chronicle retention and file modes (#186 phase 1, item 7).

  R1  DEFAULT KEEPS ALL  -- with no GNOME_SPEAKS_CHRONICLE_RETENTION_DAYS, an
                            entry a year old survives appends in every file
  R2  RETENTION PRUNES   -- with 30 days set, entries older than that are gone
                            from the active file, the rotated generation AND
                            the cold archive after the next append; newer
                            entries and an id-less line (cannot be dated) stay
  R3  THROTTLED          -- a second append inside the interval starts no prune
  R4  PRIVATE MODES      -- a ledger dir left 0775 and files left 0664 by an
                            older build are tightened to 0700 / 0600 at the
                            first append; files created fresh are 0600; a
                            symlink in place of the archive is NOT chmod'ed
                            through

Hermetic: scratch in the repo's gitignored tmp/ keyed by PID, never
~/.local/state. exit 0 = all hold; 1 = a violation.
"""
import json
import os
import shutil
import stat
import sys
import time
import traceback

# The runner discards stderr, so a crash would show only the last "ok" line.
# Put the traceback on stdout, where the runner's summary can see it.
def _crash_to_stdout(t, v, tb):
    traceback.print_exception(t, v, tb, file=sys.stdout)
    print(f"CRASH: {t.__name__}: {v}", flush=True)


sys.excepthook = _crash_to_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("GS_SVC_PATH", os.path.join(REPO_ROOT, "gnome-speaks-service.py"))
os.environ.pop("GNOME_SPEAKS_CHRONICLE_RETENTION_DAYS", None)   # R1 is the default

import harness  # noqa: E402

STATE_DIR = os.path.join(harness.SCRATCH, "retention-state")
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
    deadline = time.time() + 10
    while svc._chronicle_archiving.is_set() and time.time() < deadline:
        time.sleep(0.02)
    assert not svc._chronicle_archiving.is_set(), "background chronicle work never settled"


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def ids(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line).get("id"))
                except ValueError:
                    continue   # the planted undatable line
    except FileNotFoundError:
        pass
    return out


def plant(path, entries, file_mode=0o664):
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write((e if isinstance(e, str) else json.dumps(e)) + "\n")
    os.chmod(path, file_mode)


def fresh_ledger():
    d = os.path.dirname(svc.CHRONICLE_PATH)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    for name in os.listdir(d):   # anything a straggler wrote after the rmtree
        p_ = os.path.join(d, name)
        os.remove(p_) if not os.path.isdir(p_) or os.path.islink(p_) else shutil.rmtree(p_)
    os.chmod(d, 0o775)
    svc._chronicle_id_seeded = False
    svc._chronicle_last_id = 0
    svc._chronicle_prune_after = 0.0
    return d


assert svc.CHRONICLE_PATH.startswith(STATE_DIR), svc.CHRONICLE_PATH
now_ms = int(time.time() * 1000)
DAY = 86400 * 1000
OLD = now_ms - 365 * DAY          # a year ago
MID = now_ms - 40 * DAY           # older than a 30-day window
NEW = now_ms - 2 * DAY            # inside it
ACTIVE = svc.CHRONICLE_PATH
GEN1 = ACTIVE + ".1"
ARCHIVE = svc._chronicle_archive_path()


def plant_history():
    plant(ARCHIVE, [{"id": OLD, "kind": "you", "text": "archived old"},
                    {"id": MID, "kind": "you", "text": "archived mid"},
                    "not json at all"])
    plant(GEN1, [{"id": MID + 1, "kind": "spoken", "text": "gen1 mid"},
                 {"id": NEW, "kind": "spoken", "text": "gen1 new"}])
    plant(ACTIVE, [{"id": MID + 2, "kind": "you", "text": "active mid"},
                   {"id": NEW + 1, "kind": "you", "text": "active new"}])


# ---- R1 default: keep everything -------------------------------------------
check("R1 default retention is off", svc._CHRONICLE_RETENTION_DAYS == 0,
      f"_CHRONICLE_RETENTION_DAYS={svc._CHRONICLE_RETENTION_DAYS}")
fresh_ledger()
plant_history()
svc._chronicle_append("you", "R1 append")
settle()
check("R1 nothing pruned with retention off",
      OLD in ids(ARCHIVE) and MID + 1 in ids(GEN1) and MID + 2 in ids(ACTIVE),
      f"archive={ids(ARCHIVE)} gen1={ids(GEN1)}")

# ---- R4 modes (on the R1 ledger: planted 0664 files, 0775 dir) --------------
d = os.path.dirname(ACTIVE)
check("R4 ledger dir tightened to 0700", mode(d) == 0o700, oct(mode(d)))
for p in (ACTIVE, GEN1, ARCHIVE):
    check(f"R4 {os.path.basename(p)} tightened to 0600", mode(p) == 0o600, oct(mode(p)))

d = fresh_ledger()
os.chmod(d, 0o700)
os.umask(0o002)
svc._chronicle_append("you", "R4 fresh")
settle()
check("R4 a freshly created active file is 0600 under umask 002",
      mode(ACTIVE) == 0o600, oct(mode(ACTIVE)))

d = fresh_ledger()
victim = os.path.join(harness.SCRATCH, "retention-victim")
plant(victim, ["{}"], 0o644)
os.symlink(victim, ARCHIVE)
svc._chronicle_append("you", "R4 symlink")
settle()
check("R4 a symlinked archive is not chmod'ed through", mode(victim) == 0o644,
      oct(mode(victim)))
os.remove(ARCHIVE)

# ---- R2 retention on --------------------------------------------------------
svc._CHRONICLE_RETENTION_DAYS = 30.0
fresh_ledger()
plant_history()
svc._chronicle_append("you", "R2 trigger")
settle()
a, g, act = ids(ARCHIVE), ids(GEN1), ids(ACTIVE)
check("R2 archive: entries older than 30 days dropped", OLD not in a and MID not in a, f"{a}")
check("R2 archive: the undatable line is kept",
      "not json at all" in open(ARCHIVE, encoding="utf-8").read())
check("R2 gen .1: old dropped, new kept", MID + 1 not in g and NEW in g, f"{g}")
check("R2 active: old dropped, new + the trigger kept",
      MID + 2 not in act and NEW + 1 in act and len(act) == 2, f"{act}")
check("R2 rewritten files stay 0600",
      all(mode(p) == 0o600 for p in (ACTIVE, GEN1, ARCHIVE)))
check("R2 no temp file left behind",
      not [n for n in os.listdir(os.path.dirname(ACTIVE)) if n.endswith(".prune-tmp")])
entries = svc._chronicle_read(limit=50, q="mid", include_archive=True)
check("R2 a search no longer finds pruned text", not entries, f"{entries}")

# ---- R3 throttle ------------------------------------------------------------
plant(ARCHIVE, [{"id": OLD, "kind": "you", "text": "late old"}], 0o600)
started = svc._chronicle_prune_due_locked()
check("R3 a second prune inside the interval is not started", started is False)
check("R3 the planted old line is still there (no prune ran)", OLD in ids(ARCHIVE))
svc._chronicle_prune_after = 0.0
svc._chronicle_append("you", "R3 after interval")
settle()
check("R3 once the interval passes, the next append prunes", OLD not in ids(ARCHIVE),
      f"{ids(ARCHIVE)}")

print()
print("RESULT: " + ("RETENTION + MODES HOLD" if FAIL == 0 else f"{FAIL} FAILURES"))
sys.exit(1 if FAIL else 0)
