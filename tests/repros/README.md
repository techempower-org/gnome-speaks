# Repro suites

Plain Python scripts, **no test framework** (project rule). Each exits `0` when
clean and `1` when the defect it describes is present. Each imports the real
`gnome-speaks-service.py` in-process with the microphone, the WebSocket, D-Bus,
the injector and the network stubbed, so a run touches no device, no port and
nothing on the developer's live desktop.

```sh
tests/repros/run_all.sh                   # the service in this repo
tests/repros/run_all.sh /path/to/service.py   # a worktree, or an extracted SHA
```

## Env contract

`GS_SVC_PATH` — the `gnome-speaks-service.py` under test — is the **only** input
a suite needs, and `run_all.sh` sets it. Everything else is derived:

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
  `CONFIG_PATH`, `XDG_STATE_HOME` and `spellbook.USER_SPELLBOOK_PATH` all
  resolve inside the scratch dir.
* **No live spellbook overlay** (#152). The service merges
  `~/.config/speech-to-cli/spellbook.json` over the repo spells, so every
  harness-built service used to carry the developer's *personal* spells
  (measured: `Spellbook loaded: 24 spells` = 15 repo + 9 overlay) — read-only,
  but a spell-routing repro could match a personal incantation, and those
  spells carry the wake phrase and LAN names. `isolate_config()` now pins
  `spellbook.USER_SPELLBOOK_PATH` (the seam the service reads at construction;
  env `GS_SPELLBOOK_USER_PATH` sets it for a whole process) to an **empty**
  overlay in scratch, `assert_isolated()` asserts it, and every
  `make_service()` calls `isolation.assert_repo_spellbook()`, which counts
  what the instance actually loaded against the repo `spellbook.json`. The
  pin must land **before** `GnomeSpeaksService()` — the instance loads its
  book in `__init__`. A service whose `spellbook.py` has no seam is refused
  (setup failure), the same way an unset `CHRONICLE_PATH` is.

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
| `service-audit/repro_c9_loop_error_cap` | #117 | `a20afea` | **verified** — S1 fails (streaming: 98 cycles in 4 s, zero Errors — the silent forever-loop), B1/B2/B3 fail (batch: 1 cycle, toast on the first error, no route words — the issue's "re-enters forever" premise is *false* for batch on the baseline; it stopped, but after one hiccup). Controls B4, B5, S2 green on both sides |
| `chronicle-perf` | #22 / #27 | `6a1ecae` | derived (`merge^1`), not re-run |
| `chronicle-perf/repro_audio_info_stall` | #135 | `025df92` | **verified** — 319 ms stall, probe on MainThread |
| `injector-seam` | #24 | `ea47ff1` | derived (`merge^1`), not re-run |
| `injector-seam/repro_derive_cache` | #136 | `a20afea` | **verified** — 6 fail: 5 acquires issue 5 `list_engines()` + 5 `GetGlobalEngine` (fixed: 1 + 1; a layout switch costs one more `list_engines()`, switching back costs none); the restore/breadcrumb/no-negative-cache/per-bus checks stay green on both sides |
| `cancel-tokens` | #21 / #33 | `66edc79` | **verified** — 4 of 5 fail |
| `cancel-tokens` `repro_e` | #132 | `025df92` | **verified** — E1, E3 fail; E2 stays green on both sides |
| `cancel-tokens` `repro_f` | #132 / PR #146 review | `fd5c8cd` (PR head before revision), `0e014cc` | **verified** — F1 43/126 and 51/300 dictations hit, F2 1/1; 0/101 and clean with the fix. F1 lowers `sys.setswitchinterval` (GS_RACE_SWITCH, default 1e-5) — at CPython's 5 ms default it scored 0/300 on the unfixed tree |
| `dead-recorder` | #57, #48 / #72 | `e863b2c` | **verified** — e, f, h fail; g, i pass |
| `offline-handoff` | #49 / #70 | `70ff468` | **verified** — pre-#70 *and* pre-#72 |
| `wake-watcher` | #41, #48 / #121, #137 | `11c8f60`, `a20afea` | **verified** — pre-#41 `11c8f60`: B fails (25-spawn storm, zero sleeps). Pre-#137 `a20afea`: G fails (second recorder after 10 s of fake time, not 0.5) and H fails (a WARNING and a 10 s sleep logged *during shutdown* — the very lines #137 misread as start-time failures); B, C, F fail there too because they assert the bounded ramp before the unchanged 10 s / 60 s steady cadence. A, D, E green on every side. The fake `time.sleep` is scoped to the watcher thread by identity — the constructor also starts `tts-queue-dispatcher`, whose 0.2 s hold-polls were being recorded and stopped (A doubles as that guard: ~240 ms window, dispatcher must survive) |
| `subtitle-token` | #42 / #78 | `65bd57b` | e1–e4 fail |
| `begin-refused` | #79 / #84 | `15dd402`, `f290d7e` | j, k fail — **l stays green on both sides** |
| `tts-prefetch` | #134 (×#146 for p4) | `a20afea` | **verified** — p1 fails (wall 3.01 s serial vs 2.21 s pipelined, S=0.4/P=0.6); **p2, p3, p4 report `2` (SETUP FAILURE) there, not `0`** — the prepared-ahead window they test does not exist on a service that never prefetches, and a suite that passed on it would be measuring nothing. p4 is a guard: `skip_current()` (the #146 interrupt path) is a no-op while a reply holds the queue, and the queue path has no prefetch — it goes red if either premise changes |
| `pin-lifecycle` | #46 ×#57 | `15dd402` | compound X: X1 FAIL, X2 PASS, X3 FAIL |
| `version-cache` | #53 / #85 | two-sided, below | `577a05f` passes, `7eaeb02` fails |
| `prefs-rig` | #82 | merge base of the branch | more warnings than baseline = fail |
| `spellbook` | #119, #152 | `025df92` / `a20afea` | **verified** — 8 fail on `025df92`: `cast stop`, `cast halt`, every punctuated trigger (`Cast, stop.`, `Cast - skip`, `Invoke... skip`, …); the denylist, overlay and op-table checks stay green on both sides. The #152 seam checks (`USER_SPELLBOOK_PATH` honours `GS_SPELLBOOK_USER_PATH`; the service reads the seam, not a literal) are red on `a20afea` |
| `spellbook` | #119 | `025df92` | **verified** — 8 fail: `cast stop`, `cast halt`, every punctuated trigger (`Cast, stop.`, `Cast - skip`, `Invoke... skip`, …); the denylist, overlay and op-table checks stay green on both sides |
| `spellbook/verify_ha_token` | #129 | `a20afea` | **verified** — 7 of 8 fail (H1 default returns a token / calls `bw`, H3–H6 the config keys are ignored, H7 not synced, H8 personal literals present); H2 (env wins) green on both sides. Token values are never printed: on the maintainer's machine the baseline's personal cache file exists and the default path returns a REAL token |
| `leak-scan` | #126 | `025df92` | **verified** — 4 hits (tracked unit ×2, two plans) |
| `shell-rig` | #113 | `3c70314` | **verified** — t0 fails 8 checks: state `idle`, no service row; every later step green on both sides |
| `config-keys` | #120 #127 | `025df92` | **verified** — B fails: 8 keys against speech-to-cli before its #21 (`language`, `voice_commands` + 6 shell-only `show_*`), 6 after; D fails: the same 6 `show_*` (no Python reader); A, C pass |
| `deprecations` | #114 | `a20afea` | **verified** — 3 deprecation lines at service start (`GLib.unix_signal_add` ×2, `Gio.DBusConnection.register_object` ×1 — PyGObject warns once per deprecated GI function per process, so two register sites make one line); `GetState` and the Spiel `Name` property answer on both sides. Runs the real `main()` on a private `dbus-run-session` bus the suite re-execs itself under; `register_object` also leaked ~1.5 kB per D-Bus method call (measured; flat through `register_object_with_closures2`) |
| `install-dropins` | #115 / #103 | `a20afea` | **not re-run, by design** — that `install.sh` ignores unknown flags and would perform a full install (and restart the live service) when handed `--check-dropins`; the suite refuses any installer without the flag (SETUP FAILURE 2, verified against `a20afea`), so discrimination is by construction |

Two suites are bash and ignore `GS_SVC_PATH`, so pointing the runner at an
extracted SHA does not re-test that SHA — run them from a checkout of it:
`leak-scan` (`tests/leak-scan.sh`) scans this repo's **tracked tree**, and
`install-dropins` (`tests/repros/install-dropins/verify_dropin_warning.sh`)
drives this repo's `install.sh --check-dropins` against a scratch `$HOME` under
`tmp/repros/` — the developer's real `~/.config` is never read, and only the
check flag is invoked, so nothing is installed and no `systemctl` runs.

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
* **`tts-prefetch/prefetch_harness.py`'s two-phase fake** — it installs
  `tts_prepare`/`tts_play` AND `tts`, all writing the same event kinds, so the
  baseline (which only knows `tts()`) and the fixed service leave comparable
  timelines. Every other harness goes through `isolation.install_fake_tts()`,
  which stubs `tts` and **nulls the seam**: the service resolves the seam by
  `getattr` at call time, so a harness stubbing `tts` alone would, on a
  speech-to-cli that has the seam, send the AI-reply path at the real Azure
  URL from inside a repro.
* **`assert_isolated()`** — the harness version proves the *paths* are
  redirected; `subtitle_spy.assert_isolated()` delegates to it and adds the
  other half, that the CONFIG *pins actually took*. `assert_repo_spellbook()`
  is the same second half for the spellbook overlay: the path pin alone cannot
  see a pin applied *after* construction, only the loaded count can (#152). A path can be redirected
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

## shell-rig is a real headless gnome-shell

`tests/repros/shell-rig/run.sh [checkout]` is the other non-python suite and,
like prefs-rig, is **not** collected by `run_all.sh` (a headless shell is
~15 s of startup and needs `gnome-shell` on the machine). It installs a
checkout's `extension.js` + `stylesheet.css` into a sandboxed
`XDG_DATA_HOME`, boots `gnome-shell --headless --virtual-monitor 1280x720`
on a private bus with **no service activation**, and reads the badge from
*inside* the shell through a rig-only probe extension: computed
`St.ThemeNode` colours, accessible name, label visibility, panel-menu rows,
the notifications `Main.notify` produced. It is the instrument the "St CSS:
measure, don't reason" gotcha asks for. The timeline: no service on the bus
→ a stub owns `org.gnome.Speaks` and announces `listening` → the stub dies
→ hover / keyboard focus → a badge tap → the dictation-hotkey seam → the
stub returns idle → dies again → disable / re-enable. Exit 0 also requires
zero gnome-speaks `JS ERROR` / `CRITICAL` / St-warning lines in the shell log.

Two things it is careful about, both measured:

- **The tap spawns `systemctl --user start gnome-speaks.service` for real**,
  against a sandboxed `XDG_RUNTIME_DIR`, so it fails to connect (`Failed to
  connect to user scope bus via local transport`) and can never start, stop
  or touch the real unit. `run.sh` proves that with a control *before* the
  shell starts and aborts if the sandboxed systemctl ever reaches a manager.
  The failure is what the rig wants: it is the toast path.
- **Its bus socket is `$RUN/runtime/bus`** (`unix:runtime=yes`). The stock
  `unix:dir=` form appends `/dbus-XXXXXXXXXX` and overflowed `sun_path` from a
  worktree path — surfacing only as `dbus-run-session: EOF reading address`.
  A depth guard now fails setup with the reason instead.

The pending "Starting…" state is read by `TapAndDump`, which taps and dumps
in **one main-loop turn**: on a machine where the sandboxed spawn fails in
under 300 ms a dump scheduled "shortly after" the tap arrives too late and
reads a false negative.

## prefs-rig is not python

`prefs-rig/` is a GJS/bash rig (`run.sh` → `gjs` + `gtk4-broadwayd`) testing
`prefs.js`: 10 files, zero `.py`, by design. A `*.py` inventory returns a false
negative on it and a python collector must never try to import it. Its exit
contract is its own: **0** = both runs completed; **1** = a run did not
complete, or the branch emits *more* warnings than the baseline. A **differing
stderr is not a failure** — a fix is supposed to change stderr.
