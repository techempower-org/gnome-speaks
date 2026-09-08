# Repro suites

Plain Python scripts, **no test framework** (project rule). Each exits `0` when
clean and `1` when the defect it describes is present. Each imports the real
`gnome-speaks-service.py` in-process with the microphone, the WebSocket, D-Bus,
the injector and the network stubbed, so a run touches no device, no port and
nothing on the developer's live desktop.

```sh
tests/run-repros.sh                       # the service in this repo
tests/run-repros.sh /path/to/service.py   # a worktree, or an extracted SHA
```

## Env contract

`GS_SVC_PATH` — the `gnome-speaks-service.py` under test — is the **only** input
a suite needs, and `run-repros.sh` sets it. Everything else is derived:

⚠️ **It must be ABSOLUTE.** Each suite runs via `cd "$HERE/<suite>"`, so a
relative path resolves against the *suite* directory and every suite dies with
`FileNotFoundError` naming a path inside it. The trap is that `run_all.sh`
validates the path with `[ -f "$SVC" ]` in the *caller's* cwd, so a relative one
passes the check and fails everywhere after — validated here, used there.
`run_all.sh` now resolves its argument for you; pass an absolute path anyway if
you export `GS_SVC_PATH` yourself for a single suite.

| | |
|---|---|
| worktree dir (sibling modules: `spellbook.py`, `injector.py`, `ibus_injector.py`) | `dirname(GS_SVC_PATH)`; `GS_WT` overrides it only to mix trees deliberately |
| scratch / state | per-PID under this repo's gitignored `tmp/repros/`; `GS_REPRO_SCRATCH` pins it, and a pinned dir is never reset or deleted by a suite |
| `SPEECH_ENGINE_PATH` | `~/Projects/speech-to-cli` unless set (see CLAUDE.md, External Dependencies) |

Two rules the suites enforce on themselves, both learned the hard way on
2026-09-06:

* **No fixed scratch path.** Suites used to share absolute dirs, and two agents
  running one suite at once corrupted each other — *two concurrent runs of
  `verify_chronicle_contract.py` both failed with bogus mismatches while each
  alone passed.* It reads exactly like a service regression. Worse, a suite
  that `rmtree`s its dir at start makes the best-behaved run the most
  destructive one: a "clean" start deletes a peer's live directory. Reset and
  cleanup are therefore gated on *this process having created the dir*.
* **No live config.** `state.load_config()` reads
  `~/.config/speech-to-cli/config.json` at import and `_reload_config_flags()`
  re-reads it **mid-run** for every `_SYNC_FLAGS` key, so pinning keys after
  `load()` is silently undone at the next `start_listening()`. The harnesses
  call `_isolate_config(mod)`, which rebuilds `CONFIG` from the service's own
  defaults plus an explicit `CONFIG_PINS` dict and repoints `CONFIG_PATH` at a
  scratch file — so the live file can be neither read nor written — then
  `assert_isolated(mod)`, which **raises** unless `CHRONICLE_PATH`,
  `CONFIG_PATH` and `XDG_STATE_HOME` all resolve inside the scratch dir.

## Baselines are pinned SHAs, never branch names

A suite that diffs against a moving ref goes vacuous the moment its fix merges:
it starts comparing the fix to itself and passes forever. **Pin to a branch's
merge base, never to "current main"** — a fast-moving main manufactures false
regressions. To confirm a suite still detects what it was written for:

```sh
git archive <baseline-sha> | tar -x -C /tmp/base   # disposable
tests/repros/run_all.sh /tmp/base/gnome-speaks-service.py
```

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `service-audit` | #18–#20, #110, #124 (c8 config-watch), #130 | `7899ccb` | derived (`merge^1`), not re-run; c8 batch-error-toast verified against `025df92` (E1 fails) |
| `chronicle-perf` | #22 / #27 | `6a1ecae` | derived (`merge^1`), not re-run |
| `chronicle-perf/repro_audio_info_stall` | #135 | `025df92` | **verified** — 319 ms stall, probe on MainThread |
| `injector-seam` | #24 | `ea47ff1` | derived (`merge^1`), not re-run |
| `cancel-tokens` | #21 / #33 | `66edc79` | **verified** — 4 of 5 fail |
| `dead-recorder` | #57, #48 / #72 | `e863b2c` | **verified** — e, f, h fail; g, i pass |
| `offline-handoff` | #49 / #70 | `70ff468` | **verified** — pre-#70 *and* pre-#72 |
| `wake-watcher` | #41, #48 / #121 | `11c8f60` | **verified** — B fails (25-spawn storm, zero sleeps); A, C, D, E, F green on both sides. The fake `time.sleep` is scoped to the watcher thread by identity — the constructor also starts `tts-queue-dispatcher`, whose 0.2 s hold-polls were being recorded and stopped (A doubles as that guard: ~240 ms window, dispatcher must survive) |
| `subtitle-token` | #42 / #78 | `65bd57b` | e1–e4 fail |
| `begin-refused` | #79 / #84 | `15dd402`, `f290d7e` | j, k fail — **l stays green on both sides** |
| `pin-lifecycle` | #46 ×#57 | `15dd402` | compound X: X1 FAIL, X2 PASS, X3 FAIL |
| `version-cache` | #53 / #85 | two-sided, below | `577a05f` passes, `7eaeb02` fails |
| `prefs-rig` | #82 | merge base of the branch | more warnings than baseline = fail |
| `spellbook` | #119 | `025df92` | **verified** — 8 fail: `cast stop`, `cast halt`, every punctuated trigger (`Cast, stop.`, `Cast - skip`, `Invoke... skip`, …); the denylist, overlay and op-table checks stay green on both sides |
| `leak-scan` | #126 | `025df92` | **verified** — 4 hits (tracked unit ×2, two plans) |
| `config-keys` | #120 #127 | `025df92` | **verified** — B fails: 8 keys against speech-to-cli before its #21 (`language`, `voice_commands` + 6 shell-only `show_*`), 6 after; D fails: the same 6 `show_*` (no Python reader); A, C pass |

`leak-scan` (`tests/leak-scan.sh`, bash) is the odd one out: it scans this
repo's **tracked tree**, not `GS_SVC_PATH`, so pointing the runner at an
extracted SHA does not re-scan that SHA — run the script from a checkout of it.

`config-keys` is the one suite whose verdict also depends on a **sibling repo**:
it parses `state.py` from `SPEECH_ENGINE_PATH` (default `~/Projects/speech-to-cli`),
because the whitelist it checks against lives there. It is static — no import of
the service and no import of `state.py` (which would read the live config at
module load) — and every extractor has a positive-control floor, so a regex
that silently matches nothing is a `2`, not a pass. Its D check (every
`_SYNC_FLAGS` key has a Python reader outside the two whitelists) was PR #143's
S2, folded in here so the contract lives in one suite; #143's S1 was B.

"derived" means the SHA is the merge commit's first parent — the main tip
immediately before the fix, correct by construction but not re-run. The method
was validated on `cancel-tokens`, whose SHA was independently recorded in that
work's findings.

### Three traps in this table, all of them earned

**`begin-refused`'s `l` is green on both sides, and that is correct.** It is a
regression guard against a fix that stops speaking replies nobody cancelled —
the failure a user notices before any of the ones j and k catch. A future reader
seeing it green on main is the most likely person to delete it. Don't.

**`offline-handoff` is `70ff468`, not `79594dd`.** `79594dd` already contains
#72 (`git merge-base --is-ancestor 848b855 79594dd` → yes), so against it case I
fails for **one** reason — the offline exit doesn't exist yet, so the WS error
fires instead of the mic report — not two. `70ff468` is pre-#70 *and* pre-#72.
"Fails for two reasons" is the kind of note that stops someone investigating one
reason too early.

**`version-cache` needs two references, and here is why.** #53's fix merged as
`577a05f`; `e55e619` then deleted the hunk as plain deletions — a stale-buffer
write, no revert commit — so main went green and silently back to red before
#85 re-landed it. A suite with only *pre-fix* references can agree with itself
forever while testing nothing, which is exactly what this one did. It now runs a
two-sided control on every invocation: `GOOD_REF=577a05f` must pass,
`BAD_REF=7eaeb02` must fail (`GS_GOOD_REF`/`GS_BAD_REF` override). **Keep those
literal SHAs.** Anything resolved at runtime can silently become a second
pre-fix reference and put the suite back where it started.

## Exit codes

`0` clean · `1` the defect is present · `2` **SETUP FAILURE** — the repro could
not create the window it needed (a stalled `idle_add`, a hook that never fired),
so it is reporting neither a pass nor a bug. Several repros in `subtitle-token`
and `begin-refused` force a narrow window and then *check the window was hit*;
two early drafts passed on unfixed builds before that check existed. **A rewrite
that collapses `2` into `0` or `1` silently restores the ability to pass for the
wrong reason.**

### The pipeline-exit-status trap, in both directions

Both forms have bitten this tree, and the **silent** one is the dangerous half:

* `cmd | grep PAT | tail -1 && return` — **fails silently.** `tail` exits `0`
  on empty input, so the pipeline "succeeds" whether or not `grep` matched, the
  first branch is always taken, and every summary comes out blank. Nothing
  reports an error; you just stop being told anything. Capture into a variable
  and test it for content instead.
* a runner whose last command is a `diff` or `grep` — **fails loudly**, and
  returns `1` on a *successful* run (a fix is supposed to change stderr). At
  least someone notices, usually when they gate CI on it.

Same root cause: taking a pipeline's exit status as the answer to a question it
never answered. `$?` after a pipe reads the LAST command, not the
interesting one — that form once reported two red suites as green here.

**It came back, in the one branch I hand-rolled instead of using my own fix.**
After `summarise()` was written to capture-and-test, the prefs-rig branch of
`run_all.sh` kept a private `grep -E 'EXIT_CLEAN|REGRESSION' | tail -1`. Neither
pattern matches what the rig prints when it cannot *start*, so when prefs-rig
became the one check that actually flaked, the runner reported `rc=1` followed
by nothing at all — a red that named no reason, in the file whose job is to
make a red legible. The fix existed three lines above the bug.

Two rules came out of that, and both are asserted by control rather than
assumed:

* **`^!!` is matched first.** Suites emit `!! SETUP FAILURE` / `!! REGRESSION` /
  `!! HARNESS DID NOT COMPLETE` for what a reader must not miss, and a run can
  limp as far as a `[PASS]` line and *then* fail setup. If the case lines won,
  that run would report the pass.
* **A non-zero rc never prints a blank diagnostic.** The last-resort fallback
  was `tail -1`, which returns empty on empty input — so "always prints a
  diagnostic" was only *mostly* true until an explicit
  `!! NO OUTPUT from <suite> (rc=N)` closed it. `$?` after a pipe reads the LAST command, not the interesting
one — that form once reported two red suites as green here.

⚠️ **And do not "fix" this by reaching for `set -euo pipefail`, which is the
obvious next move after reading the above.** `set -o pipefail` alone is right and
all five runners now set it. **`-e` is not**: `grep -c` exits `1` when the count
is `0`, and a zero count is the *passing* case for a warning counter, so `-e`
aborts a run precisely when it succeeds — measured on `prefs-rig/run.sh`, whose
two `grep -c` lines it kills on a clean run. The hardening against the silent
form has its own short-form failure. If you do add `-e`, write every count as
`$(grep -c X f || true)` first.

## Instruments that must survive a refactor

* **`subtitle-token/subtitle_spy.py: GLibSpy`** — the harness runs no GLib main
  loop, so `_emit_subtitle_update` callbacks queue and never fire. A subtitle
  assertion written without the shim reads an empty list, measures nothing and
  **passes**. It is the instrument, not a convenience.
* **`assert_isolated()`** — the harness version proves the *paths* are
  redirected; `subtitle_spy.assert_isolated()` delegates to it and adds the
  other half, that the CONFIG *pins actually took*. A path can be redirected
  correctly while a pin is silently missing, and `terminal_mode` really is
  `True` on the developer desktop. Keep both halves if these are ever folded
  together.
* **`prefs-rig` containment comes from `GLib.get_home_dir()` honouring `$HOME`**,
  not from its monkeypatched writers. The stubs are a *reporting* mechanism (they
  produce the `WRITES` line), not the safety mechanism — proven by
  `probe_write_containment.js`, which removes the stub and drives the real
  writer with a sentinel. So deleting a stub to exercise a save path is safe.
* **`offline-handoff` case H** replaces `stt.wyoming.transcribe` and never
  restores it; it is safe only because `build()` re-stubs it every call. Case G
  restores `_rest_stt_fallback` explicitly because `build()` does *not*. The
  runner gives each case its own process so this is irrelevant rather than
  merely currently-true — if stub ownership ever moves into the harness, check H
  before trusting it.

## Orphaned broadwayds, and a control that can fail

A hard-killed `run.sh` (SIGKILL from a tool timeout, or an OOM kill) leaves its
`gtk4-broadwayd` **alive** — the EXIT trap covers TERM and INT, but nothing
catches KILL. A live orphan is not stale: the liveness probe correctly reads it
as *occupied* and skips that display forever, so orphans **monotonically
exhaust** the 26-slot probe window. Same shape as the stale-socket bug, a
different cause, and it survives that fix.

`run.sh` reaps them before probing. PIDs are resolved **through the socket** —
socket path → inode from `/proc/net/unix` → the process holding that inode in
`/proc/<pid>/fd` — and never by matching a process name. A name pattern is how
a reaper kills its own shell: bracketing the first character protects the
pattern from matching *itself*, but not from the bare token appearing elsewhere
in the same command. There is no name here for a pattern to match.

Two restrictions keep it off anything that is not ours: the process must hold
one of our sockets, **and** its stdout must be a `.../run-<pid>/broadwayd.log`
that no longer exists. A live run's log exists; a hand-started broadwayd has a
different stdout. Both are verified, not assumed.

`verify_reaper.sh` is the control, and it is checked **in both directions**:

```
A. reaper DISABLED  -> rc=2  !! SETUP FAILURE: no free broadway display in :200..:225
B. reaper ENABLED   -> rc=0  26 orphans reaped
```

> The candidate control for the sibling socket bug returned `0 -> 0` against
> **unfixed** code — it would have passed on the bug it was meant to catch, and
> was dropped for that reason. **A gate that is green on broken code
> manufactures confidence.** If a control has never been watched to fail, it is
> not yet a control.

It is not wired into `run_all.sh`: it starts 26 processes and verifies the
reaper, not the service. Run it when touching the probe or the reaper.

## prefs-rig is not python

`prefs-rig/` is a GJS/bash rig (`run.sh` → `gjs` + `gtk4-broadwayd`) testing
`prefs.js`: 10 files, zero `.py`, by design. A `*.py` inventory returns a false
negative on it and a python collector must never try to import it. Its exit
contract is its own: **0** = both runs completed; **1** = a run did not
complete, or the branch emits *more* warnings than the baseline. A **differing
stderr is not a failure** — a fix is supposed to change stderr.
