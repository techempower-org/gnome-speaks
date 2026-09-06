#!/usr/bin/env python3
"""Verification suite for issue #53 — /api/version must not fork git per request.

    GS_SVC_PATH=<service file> python3 verify_version_cache.py

Checks
  1. the fork-storm repro (repro_a) passes on this file
  2. the payload is field-for-field identical to the PRE-FIX baseline's
     (GS_BASELINE_REF, default 7eaeb02), apart from the four fields that are
     per-process by definition (uptime, pid, built, started).  Comparing against
     a moving origin/main goes vacuous the moment the fix lands there.
  3. `uptime` is still live — it tracks _SERVICE_START_TIME, it is not frozen
     into the cache
  4. the realm-sigil-absent fallback still works, is cached too, and keeps a
     live uptime
  5. concurrent requests build the payload exactly once (ThreadingHTTPServer)

exit 0 = clean.
"""
import json
import os
import shutil
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from harness import CountingSubprocess, VersionClient, load  # noqa: E402

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

SVC_PATH = os.environ.get(
    "GS_SVC_PATH", SVC_DEFAULT)
MAIN_REPO = REPO_ROOT
# The ref the payload is compared against.  Default origin/main -- but once the
# #53 fix landed on main that comparison became a TAUTOLOGY (the fix vs itself),
# so the durable proof pins the last PRE-FIX commit.  Override with
# GS_BASELINE_REF=<ref> when re-checking against a moving main is what you want.
BASELINE_REF = os.environ.get("GS_BASELINE_REF", "7eaeb02")

# ---------------------------------------------------------------------------
# THE TWO-SIDED CONTROL -- do not remove, and do not let both refs be pre-fix.
#
# 577a05f is the ONLY commit where /api/version is correct: #53 merged there and
# was silently overwritten one commit later by e55e619 (a stale-buffer write, no
# revert commit -- `--contains` and "PR merged" both still answer YES).  With
# only pre-fix references a suite can agree with itself forever while testing
# nothing, which is exactly what this one did until 2026-09-06.
#   GOOD_REF must PASS, BAD_REF must FAIL, every run.
# ---------------------------------------------------------------------------
GOOD_REF = os.environ.get("GS_GOOD_REF", "577a05f")   # #53 fix, as merged
BAD_REF = os.environ.get("GS_BAD_REF", "7eaeb02")     # last pre-fix main
from harness import SCRATCH  # noqa: E402  (per-PID; see the harness isolation note)

BASELINE_DIR = os.path.join(SCRATCH, "baseline")
PER_PROCESS = {"uptime", "pid", "built", "started"}

FAILURES = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + (("  -- " + detail) if detail else ""))
    if not ok:
        FAILURES.append(label)


def dump(svc_path):
    env = dict(os.environ, GS_SVC_PATH=svc_path)
    out = subprocess.run([sys.executable, os.path.join(HERE, "dump_payload.py")],
                         capture_output=True, text=True, env=env, timeout=120)
    if out.returncode != 0:
        raise RuntimeError("dump_payload failed for %s:\n%s" % (svc_path, out.stderr[-2000:]))
    return json.loads(out.stdout.strip().splitlines()[-1])


def snapshot(ref, subdir="baseline"):
    """A historical service file in a dir where its siblings still resolve."""
    BASELINE_DIR = os.path.join(SCRATCH, subdir)
    os.makedirs(BASELINE_DIR, exist_ok=True)
    for name in os.listdir(MAIN_REPO):
        if name.endswith((".py", ".json")) and name != "gnome-speaks-service.py":
            link = os.path.join(BASELINE_DIR, name)
            if not os.path.lexists(link):
                os.symlink(os.path.join(MAIN_REPO, name), link)
    dest = os.path.join(BASELINE_DIR, "gnome-speaks-service.py")
    # NB: the siblings are symlinked from the CURRENT tree while the service
    # file comes from BASELINE_REF.  Fine for this suite (nothing in
    # _handle_version touches them), but it is a mixed checkout -- do not reuse
    # this dir for a repro that exercises the injector or the spellbook.
    blob = subprocess.run(
        ["git", "-C", MAIN_REPO, "show", "%s:gnome-speaks-service.py" % ref],
        capture_output=True, text=True, timeout=60)
    if blob.returncode != 0:
        raise RuntimeError("cannot read %s service file: %s" % (ref, blob.stderr))
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(blob.stdout)
    return dest


def main():
    print("service under test: %s\n" % SVC_PATH)

    # -- 0. DISCRIMINATION: this suite must disagree with itself across a
    #        known-good and a known-bad snapshot, or its verdict is worthless.
    for ref, label, want_ok in ((GOOD_REF, "known-GOOD", True),
                                (BAD_REF, "known-BAD", False)):
        try:
            svc = snapshot(ref, "control-%s" % ref)
        except RuntimeError as exc:
            check("control snapshot %s (%s) is reachable" % (ref, label), False, str(exc))
            continue
        rc = subprocess.run([sys.executable, os.path.join(HERE, "repro_a_fork_storm.py")],
                            env=dict(os.environ, GS_SVC_PATH=svc),
                            capture_output=True, text=True, timeout=300)
        forks = next((l for l in rc.stdout.splitlines() if l.startswith("git processes")), "?")
        check("positive/negative control: %s %s must %s" % (
                  ref, label, "PASS" if want_ok else "FAIL"),
              (rc.returncode == 0) == want_ok, forks)

    # -- 1. the repro -------------------------------------------------------
    rc = subprocess.run([sys.executable, os.path.join(HERE, "repro_a_fork_storm.py")],
                        env=dict(os.environ, GS_SVC_PATH=SVC_PATH),
                        capture_output=True, text=True, timeout=300)
    check("repro_a_fork_storm passes", rc.returncode == 0,
          rc.stdout.strip().splitlines()[-1] if rc.stdout.strip() else rc.stderr[-300:])

    # -- 2. contract identity vs origin/main --------------------------------
    mine = dump(SVC_PATH)
    theirs = dump(snapshot(BASELINE_REF))
    check("key order identical to baseline (%s)" % BASELINE_REF,
          list(mine.keys()) == list(theirs.keys()),
          "%s vs %s" % (list(mine.keys()), list(theirs.keys())))
    diff = {k: (theirs.get(k), mine.get(k))
            for k in set(mine) | set(theirs)
            if k not in PER_PROCESS and mine.get(k) != theirs.get(k)}
    check("every stable field identical to baseline (%s)" % BASELINE_REF,
          not diff, repr(diff))
    check("the per-process fields are all still present",
          PER_PROCESS <= set(mine), "missing %s" % sorted(PER_PROCESS - set(mine)))
    check("uptime/pid are ints in both",
          all(isinstance(p[k], int) for p in (mine, theirs) for k in ("uptime", "pid")))

    # -- 3. uptime is live, not frozen into the cache -----------------------
    mod = load()
    client = VersionClient(mod)
    first = dict(client.get(1))
    mod._SERVICE_START_TIME -= 42          # as if 42 s of service life passed
    second = client.get(1)
    check("uptime advances with service life (not cached)",
          second["uptime"] - first["uptime"] >= 42,
          "%s -> %s" % (first["uptime"], second["uptime"]))
    check("uptime advanced without re-forking git",
          client.counter.git_count() <= 3, "%d forks" % client.counter.git_count())

    # -- 4. realm-sigil-absent fallback -------------------------------------
    mod2 = load2 = None
    sys.modules["realm_sigil"] = None      # makes `from realm_sigil import ...` raise
    try:
        mod2 = load()
        fb = VersionClient(mod2)
        p1 = dict(fb.get(1))
        forks_after_first = fb.counter.git_count()
        mod2._SERVICE_START_TIME -= 7
        p2 = fb.get(9)
        check("fallback payload keeps its minimal contract",
              set(p1) == {"name", "version", "hash", "branch", "dirty", "uptime"},
              repr(sorted(p1)))
        check("fallback used the real git facts",
              (p1["hash"], p1["branch"]) == (fb.counter.hash, fb.counter.branch),
              repr((p1["hash"], p1["branch"])))
        check("fallback is cached too (10 requests, <= 3 forks)",
              fb.counter.git_count() <= 3,
              "%d after 1, %d after 10" % (forks_after_first, fb.counter.git_count()))
        check("fallback uptime stays live", p2["uptime"] - p1["uptime"] >= 7,
              "%s -> %s" % (p1["uptime"], p2["uptime"]))
    finally:
        sys.modules.pop("realm_sigil", None)

    # -- 5. concurrent pollers build it exactly once ------------------------
    mod3 = load()
    counter = CountingSubprocess()
    clients = [VersionClient(mod3, counter) for _ in range(8)]
    mod3.SpeechHTTPHandler._version_cache = None
    errors = []

    def hammer(c):
        try:
            for _ in range(25):
                c.get(1)
        except Exception as exc:      # pragma: no cover - a race would land here
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    check("8 threads x 25 requests raised nothing", not errors, repr(errors[:2]))
    check("8 threads x 25 requests forked git at most 3 times",
          counter.git_count() <= 3, "%d forks" % counter.git_count())
    payloads = {json.dumps({k: v for k, v in p.items() if k != "uptime"}, sort_keys=True)
                for c in clients for p in c.payloads}
    check("every thread saw the same payload", len(payloads) == 1,
          "%d distinct payloads" % len(payloads))

    print("\n%s" % ("FAILURES: " + ", ".join(FAILURES) if FAILURES else "all checks passed"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(BASELINE_DIR, ignore_errors=True)
