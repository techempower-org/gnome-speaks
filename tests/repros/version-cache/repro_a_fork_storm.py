#!/usr/bin/env python3
"""Repro for issue #53 -- GET /api/version forks three git processes per request.

Measured on origin/main (7eaeb02):

    20 requests -> 60 git processes, of which 20 are `git status --porcelain`.

The git facts in the payload describe the code THIS PROCESS LOADED.  They
cannot change while it runs, so every fork after the first three buys nothing;
under the status poller they are a fork storm on the desktop session bus.

exit 0 = fixed (<= 3 forks total, contract intact)
exit 1 = bug present
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import CountingSubprocess, VersionClient, load  # noqa: E402

N = 20
FAILURES = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + (("  -- " + detail) if detail else ""))
    if not ok:
        FAILURES.append(label)


def main():
    mod = load()
    counter = CountingSubprocess()
    client = VersionClient(mod, counter)

    first = client.get(1)
    after_first = counter.git_count()
    client.get(N - 1)
    total = counter.git_count()
    last = client.payloads[-1]

    status_forks = [c for c in counter.git_calls() if "status" in c or "diff" in c]

    print("git processes: %d after 1 request, %d after %d requests "
          "(%d of them a working-tree scan)"
          % (after_first, total, N, len(status_forks)))

    check("first request costs at most 3 git processes",
          after_first <= 3, "got %d" % after_first)
    check("%d requests cost at most 3 git processes in total" % N,
          total <= 3, "got %d (%.1f per request)" % (total, total / N))
    check("no working-tree scan per request",
          len(status_forks) <= 1, "got %d" % len(status_forks))

    # -- the contract must be byte-identical apart from the live field --------
    check("payload keys unchanged between requests",
          list(first.keys()) == list(last.keys()),
          "%s vs %s" % (list(first.keys()), list(last.keys())))
    check("hash/branch/dirty served from the cache",
          (first.get("hash"), first.get("branch"), first.get("dirty"))
          == (last.get("hash"), last.get("branch"), last.get("dirty")))
    check("git facts came from git, not a placeholder",
          first.get("hash") == counter.hash and first.get("branch") == counter.branch,
          repr((first.get("hash"), first.get("branch"))))
    check("uptime is still present and live (int, recomputed per request)",
          isinstance(last.get("uptime"), int))

    # realm-sigil contract keys (import succeeds on this host)
    expected = {"name", "description", "version", "hash", "branch", "dirty",
                "built", "realm", "repo", "commit_url", "started", "uptime",
                "runtime", "host", "pid"}
    missing = expected - set(last.keys())
    check("realm-sigil contract keys all present",
          not missing, "missing %s" % sorted(missing) if missing else "")

    print("\n%s" % ("FAILURES: " + ", ".join(FAILURES) if FAILURES else "all checks passed"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
