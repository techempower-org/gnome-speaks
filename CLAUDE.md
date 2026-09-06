<!-- claude-md-version: c2e0cdd | updated: 2026-03-22 -->
# CLAUDE.md — gnome-speaks

GNOME Shell extension (v46-50) for desktop voice interaction: STT, TTS, and AI conversation via Azure Speech Services.

## Architecture

Two-process design connected by session D-Bus (`org.gnome.Speaks`):

| File | Runtime | Role | Lines |
|------|---------|------|-------|
| `extension.js` | GNOME Shell (GJS) | UI: badge + pills + 📜 rune, panel indicator, subtitle overlay, chronicle scroll, keybindings, drag | ~2,620 |
| `gnome-speaks-service.py` | systemd user service (Python) | Audio, STT, TTS, speech queue, chronicle, LLM, typing, clipboard, wake watcher | ~3,720 |
| `injector.py` | imported by the service | The `Injector` seam: the contract both injection backends implement | ~120 |
| `ibus_injector.py` | imported by the service | `IbusInjector` — text injection as an IBus engine (D-Bus commits, preedit, crash recovery) | ~660 |
| `spellbook.py` | imported by the service | Incantation matcher + executor ("cast …" → local actions); denylist | ~390 |
| `spellbook.json` | data | 15 repo spells (self-control); user overlay at `~/.config/speech-to-cli/spellbook.json` merges + hot-reloads | — |
| `spiel_provider.py` | imported by the service | Spiel/libspiel synthesis side (`org.gnome.Speaks.Speech.Provider`); off unless `spiel_provider` | ~120 |
| `prefs.js` | GNOME Extensions app (GJS/Gtk4) | 6-page preferences window (task-first redesign, #5e49049) | ~1,540 |
| `stylesheet.css` | GNOME Shell | Badge states, pills, animations, subtitle overlay, chronicle scroll | ~560 |

The extension touches **no** network or audio -- all I/O is in the Python service.
Spoken output serializes through a FIFO **speech queue** (HTTP callers never stomp
each other; user speech preempts). Transcripts hit the **spellbook** before mode
routing. Everything said in either direction appends to the **chronicle**
(`$XDG_STATE_HOME/gnome-speaks/chronicle.jsonl`) and is respeakable. A **wake
watcher** thread streams mic audio to a LAN Wyoming openwakeword server while idle.

## External Dependencies

Two sibling projects are imported at runtime (not pip packages):

- **speech-to-cli** (`~/Projects/speech-to-cli`, env `SPEECH_ENGINE_PATH`) -- provides `state`, `audio`, `stt`, `speech_tts`, `wyoming` modules. `wyoming` carries the LAN offline fallback (Piper TTS / local STT with a 60s Azure circuit breaker; `SPEECH_FORCE_OFFLINE=1` forces it) and `detect_stream` for the wake word. **Import order gotcha**: these modules are importable only after the `sys.path.insert` for `SPEECH_ENGINE_PATH` (~line 58) — imports above it fail at service start.
- **cloud-chat-assistant** (`~/Projects/cloud-chat-assistant`, env `CLOUD_CHAT_PATH`) -- optional Bedrock/Azure LLM backend

Config files:
- `~/.config/speech-to-cli/config.json` -- Azure keys, STT/TTS settings, mode flags
- `~/.config/cloud-chat-assistant/config.json` -- Azure AI / Bedrock credentials

## Build & Install

```bash
./install.sh          # copies to ~/.local/share/gnome-shell/extensions/, compiles schemas, starts service
./install.sh -u       # uninstall
```

After editing extension.js, prefs.js, or stylesheet.css, re-run `./install.sh` and restart GNOME Shell (log out/in on Wayland).

After editing gnome-speaks-service.py only:
```bash
systemctl --user restart gnome-speaks.service
```

No separate build step -- files are plain JS and Python (no transpilation, no bundling).

`./pack.sh` builds the extensions.gnome.org submission zip via the official
`gnome-extensions pack` into `dist/` (gitignored). Extension only -- the service
is not a shell component and never ships in that zip.

## Service Management

```bash
systemctl --user status gnome-speaks.service
systemctl --user restart gnome-speaks.service
journalctl --user -u gnome-speaks.service -f    # live logs
```

HTTP REST API on `localhost:7710`: `POST /speak` (queues FIFO; `interrupt:true`
flushes; `source` + `coalesce`/`kind:"progress"` drops that source's own
unspoken backlog so agents never narrate stale status), `/skip` (optional
`{"id":N}` scopes it to that item), `/stop` (drains queue), `/pause`,
`/resume`, `/cast` (text seam into the spellbook — same gates as spoken casts),
`/respeak` (`{"id":N}`; omit id = last spoken line), `GET /status`, `/queue`
(pending + `source` + per-item outcomes: done/canceled/interrupted/error),
`/voices`, `/chronicle` (`?limit&q&kind=you|spoken`, oldest-first),
`/api/version` (realm-sigil contract).

## D-Bus Interface

Bus name: `org.gnome.Speaks` | Path: `/org/gnome/Speaks`

Key methods: `StartListening`, `StopListening`, `Speak(text)`, `SpeakClipboard`, `SpeakSelection`, `Talk(text)`, `Stop`, `GetState`, `GetChronicle(limit)` (`limit<=0` → 20), `Respeak(id)` (`0` → last spoken)

Second bus name when `spiel_provider` is enabled: `org.gnome.Speaks.Speech.Provider` (`org.freedesktop.Speech.Provider`, see `spiel_provider.py`).

Signals: `StateChanged`, `TranscriptionReady`, `PartialTranscription`, `SubtitleUpdate`, `AudioLevel`, `Error`

Test from CLI:
```bash
dbus-send --session --dest=org.gnome.Speaks --print-reply /org/gnome/Speaks org.gnome.Speaks.GetState
```

## LLM Providers

8 providers configured via prefs. `MODEL_MAP` dict in gnome-speaks-service.py translates canonical model names to provider-specific IDs.

Streaming (sentence-level TTS): local (OpenAI-compatible LAN server, e.g. Qwen via llm_stream `local_endpoint`), Anthropic, OpenAI, Azure AI, Google, DigitalOcean, Puter
Synchronous fallback: cloud-chat-assistant, Bedrock

## Modes

| Mode | What it does |
|------|-------------|
| Type (default) | STT -> typed at cursor via ydotool |
| AI | STT -> LLM -> TTS (streaming sentence-level) |
| Loop | Auto-restart listening after each utterance |
| Terminal | Lowercase, no punctuation, lexical output |
| Talk | D-Bus API for external apps (blocking call) |
| Half/Full Duplex | Auto-detected speaker vs headphone routing |
| Wake word | Idle-only mic stream to LAN openwakeword; detection = dictation hotkey. Toggle: "cast wake word". Opt-in `wake_word_secure_gate` (prefs: "Only Type Into Known Fields"): a wake-opened session types only into a focused field the app has **not** declared PASSWORD/PIN — ONE verdict per session, taken where `live_typing` is computed (gating just the final paste missed live partials and Keep-Live-Text, #55); `start_listening(quick=True)` keeps the wake mark. It **fails open**: ibus-daemon 1.5.34 always forwards `SetContentType`, and an undeclared field arrives as `(0,0)` = FREE_FORM, so an undeclared password box is undetectable. Works on native Wayland (measured GNOME 50) — it is not X11-only. `python3 verify_wake_gate.py` checks it |
| Injection | How text reaches the cursor. `injection_method`: `ydotool` (default, synthesizes keys, learns nothing about the target) · `ibus` (D-Bus commits, no stuck keys, sees the content-type the app declares — so it can skip a declared password/PIN field, never an undeclared one) · `auto` (ibus when reachable). Falls back to ydotool for every failure, never to nothing |
| Spellbook | "cast …"/"invoke …" transcripts run local spells (never typed/LLM'd); `POST /cast` is the text seam |
| Chronicle | Not a mode -- always-on ledger of both directions; 📜 badge rune (8 lines) + panel submenu (12), click to respeak. Spells: "cast echo" / "chronicle" / "seal the chronicle" |

## Coding Conventions

- **extension.js**: GJS with GNOME Shell imports (St, Clutter, Meta, Shell). No ES modules from npm -- pure GObject Introspection. Prefix private methods with `_`.
- **gnome-speaks-service.py**: GLib main loop + threading for blocking audio/network ops. `GLib.idle_add()` to marshal D-Bus signal emissions back to the main thread. Logs to stderr via `logging`.
- **prefs.js**: Adw (libadwaita) preferences pages. Config changes written to `~/.config/speech-to-cli/config.json` with debounced, **merge-on-write** saves (#16): re-read the file and apply only the dirty/deleted keys, because the service writes the same file (spells, quality toggle) and dumping a stale full object would erase its changes. Track edits through `_setConfigValue`/`_deleteConfigKey` so they land in `_dirtyKeys`/`_deletedKeys` -- mutating `this._config` directly means the change is silently dropped at save time.
- **stylesheet.css**: GNOME Shell CSS (subset of CSS3). No SCSS or preprocessors.

## Key Gotchas

- **IBus crash state is "no input method", not "the wrong one"**: measured on GNOME 50.1 — if the
  service dies between `SetGlobalEngine(ours)` and the restore, the global engine is left **empty**
  and the daemon does **not** auto-revert. On a desktop where the keymap comes from an IBus engine
  that can mean no working keyboard. Hence the four mitigations in `ibus_injector.py`
  (`$XDG_RUNTIME_DIR/gnome-speaks/prior-engine` written *before* the swap, restore-on-start,
  `ExecStopPost=… --restore-ime`, and a session watchdog). Do not treat any of them as optional,
  and keep `restore_prior_engine()` dependency-free — it must run with no config and no instance.
- **The cancel wire is not the cancel verdict**: `state._cancel_event` (speech-to-cli) is a single
  process-global bit shared by every STT and TTS call. It can *interrupt* an operation, but it can
  never say **which** one was cancelled, and the next operation's `CancelRegistry.begin()`
  legitimately lowers it while an earlier one is still winding down (stop's joins time out after
  3 s). So anything the service decides **about a finished operation** — type this transcript,
  record this queue outcome, restart the loop — must read that operation's `CancelToken`, never the
  wire. Never reintroduce a bare `state._cancel_event.clear()` in a worker; go through
  `self._cancels` (`issue` → `begin` → `retire`), and retire on every exit or the token leaks into
  the live set forever.
- **`stop()` and `stop_listening()` both set `_stop_event` and mean opposite things**:
  `stop_listening()` is the dictation hotkey — end the utterance, **keep** the text.  `stop()` is the
  panic stop (D-Bus Stop, `POST /stop`, "cast stop", every user-speech preemption) — **abandon** it.
  `_stop_event` cannot express the difference and is cleared by the next `start_listening()`, so
  only `stop()` cancels the session token and only the token may gate the transcript. A library
  result is not a cancellation check either: `stt_fixed()` calls `is_cancelled()` exactly once and
  then POSTs to Azure with a 30 s timeout, so a stop during the upload is simply never seen.
- **`commit_text("\n")` is not the Enter key**: it inserts a newline *character*, so a shell never
  runs the command. `press_enter()` is a distinct seam method for this reason and delegates to a
  key-event backend even when IBus is active (spec §5.4, "non-text targets"). Never collapse it
  into `commit`/`type_raw`.

- **The streaming STT path has no offline seam of its own** (#49): while `wyoming_mod.skip_azure()`
  holds (60 s breaker or `SPEECH_FORCE_OFFLINE=1`), `start_listening()` routes a streaming session
  to the VAD batch path, and a WS connect failure calls `mark_azure_down()` and hands the session to
  `_offline_stt_session` → `_rest_stt_fallback`. With Wyoming configured the WS gets ONE attempt.
- **A recorder nobody is reading is a recorder losing speech.** The pipe between `pw-record` and this
  process holds **65536 B = 2.048 s** of 16 kHz mono PCM (measured, `F_GETPIPE_SZ`), and a WS connect
  attempt can last **10 s** — so audio spoken past the first two seconds of a stalled connect was
  destroyed *at the source*, and no amount of cleverness at the handoff could get it back. Hence
  `_RecorderTap`: a reader thread owns `proc.stdout` from the moment the recorder starts, so the
  recorder never blocks and a failed connect can hand the WHOLE utterance to Wyoming. Consumers read
  frames from the tap, not the pipe (`calibrate_noise(tap)`, `tap.read(FRAME_BYTES)`); between loop
  cycles it is trimmed to `_PIPE_FRAMES` so the next utterance does not start with the service's own
  TTS. Never reintroduce a bare `proc.stdout.read()` in the streaming cycle. **The tap never touches
  `proc`, so `proc.poll()` stays the positive control for a lost mic** — and because EOF surfaces only
  after the buffer drains, the words already captured are delivered before it (#57/#48).
- **`_offline_stt_session` drains BEFORE it honors `stopping()`** — and the order is the fix, not a
  detail. The dictation hotkey firing while a connect is still pending is the NORMAL case, and it
  means "finish this utterance", not "discard it"; a version that checked `stopping()` first handed
  the recognizer 960 B — one calibration frame — and silently lost the press. Only `stop()` discards,
  it says so by cancelling the token, and that verdict is applied once, in `_deliver_stt_result`.
- **One session, at most one toast, and never instead of the words.** Both streaming exits report
  through `_report_recorder_dead()`; a lost mic outranks "STT WebSocket failed", and text that
  actually reached the cursor outranks both (an error notification next to text landing under the
  cursor is a lie about the outcome). Keep the ranking in one place if a third failure is added.

- **ydotool stuck keys**: If a ydotool command is interrupted between key-down and key-up, the virtual device retains that key as pressed. The service auto-restarts `ydotoold` to recover. Scripts: `fix-ydotool.sh`, `install-ydotool.sh`.
- **pw-record ignores SIGTERM**: Must use SIGKILL (`proc.kill()`) to stop PipeWire recorder processes.
- **Half-duplex drain**: On speakers, 0.5s delay after TTS before opening mic to prevent echo pickup.
- **Config dual-write**: Mode flags exist in both the Python `CONFIG` dict (runtime) and `~/.config/speech-to-cli/config.json` (disk). `_reload_config_flags()` and `_save_config_flag()` keep them in sync. Be careful not to create drift.
- **Schema compilation**: After editing the `.gschema.xml`, must run `glib-compile-schemas` on the install directory.
- **Disposed notification sources**: During shell init/restart, `MessageTray` `source-added` can fire with already-disposed `FdoNotificationDaemonSource` objects. Any signal connection on them crashes the shell. Always wrap `source.connect()` in try-catch and listen for `source-removed` to drop references before GC disposes them.
- **Azure content filter**: Avoid `[SYSTEM:]` prefix in system prompts -- Azure GPT content filter blocks it.
- **speech-to-cli `load_config()` whitelists keys**: unknown config.json keys are silently dropped. Adding a config key means adding it to the whitelist in `state.py` too, or the feature reads a default forever. Scope: this only binds keys the **Python** side reads -- keys consumed only by extension.js (`subtitles_user`, `subtitles_tts`) bypass it entirely, since GJS parses config.json raw. Don't "fix" their absence from `state.py`, and don't assume a working extension key means the Python side can see it.
- **`Shell.Eval` is dead** (returns `(false,'')`; Introspect/Screenshot are AccessDenied) -- the service cannot ask the compositor anything directly (#7). The replacement is the `org.gnome.Speaks.Desktop` interface **exported by extension.js** (`GetFocusedApp` → `wm_class, title`), which `_get_focused_app()` calls via `gdbus`; it works only while the extension is loaded and returns `None` headless. Any further desktop actuation goes the same way: a method on that interface, answered from inside the Shell.
- **Public repo**: LAN hostnames/IPs, the HA domain, and the wake-word model name (it's the wake phrase) never enter git -- they live in `~/.config/speech-to-cli/config.json` and the user spellbook overlay. Scan patch history before pushing.
- **systemctl scope trap**: this file prescribes `systemctl --user` for the voice service — but `systemctl --user is-active <system-unit>` answers `inactive` with **exit 0** for units that live in the system scope (e.g. litrpg-engine on this machine). A confidently wrong answer; check the scope before believing "inactive", and never build a health check or spell on the --user reading of a system unit.
- **Speech-queue state ownership**: `_speak_token` fences playback cleanup -- a preempted worker must not reset state it no longer owns. Keep the token claims when adding new speech paths.
- **St CSS: measure, don't reason.** Specificity arithmetic on paper produced two wrong (and confidently shipped) conclusions in one day (2026-08-18): pill text was believed white (it was state-tinted by later type selectors) and a "(0,2,1)" counter-rule was really (0,1,1) and inert in 4 of 5 states. The instrument that works: dump computed `St.ThemeNode` values (foreground color, margins) from a headless shell per state and diff before/after. Badge labels are addressed by NAME (`gnome-speaks-badge-label`, `gnome-speaks-pill-label`); never reintroduce `StLabel` type selectors -- pill text tints by INHERITANCE from its pill class.
- **`--nested` is gone on GNOME 50**: the nested-shell test harness is now `dbus-run-session -- gnome-shell --headless --virtual-monitor 1280x720`. Any doc, script, or muscle memory reaching for `--nested` fails on 50+.
- **`addTopChrome` and `affectsInputRegion`**: GNOME 49+ tracks input regions from reactive actors automatically and **rejects** the param. It defaulted to `true` on 46-48, so omitting it is behavior-identical everywhere -- never re-add it.
- **St renders only ONE box-shadow**: comma-separated shadow lists log `Ignoring excess values` per rule and the extra layers never draw. Keep one shadow per rule and get depth from gradients instead.
- **`log()` is deprecated in GJS**: use `console.log/warn/error/debug`. Service-absent paths should log at `debug` so a solo-extension install (EGO users with no service) stays quiet.
- **`PopupSubMenu.open()` refuses an EMPTY submenu** (`popupMenu.js` guards on `isEmpty()`): populating a submenu from its own `open-state-changed` deadlocks -- the event never fires, the row is dead. Seed a placeholder at build time and refresh from the PARENT menu's open instead (the Chronicle submenu bug, cff6745).
- **Subtitles are conversation-mode only**: both user-voice subtitle paths early-return on `!this._conversationMode` (in dictation the text is already at the cursor). `subtitles_user` / `subtitles_tts` gate the two directions independently *on top of* the `live_subtitles` master; `live_subtitles` is dual-written to GSettings `live-subtitles` because the overlay gates on the GSettings layer.
- **Orca's Spiel switch moved**: `orca.settings.speechSystemOverride` **no longer exists** in Orca 50 -- an `orca-customizations.py` setting it does nothing silently. Use the relocatable GSettings schema: `gsettings set "org.gnome.Orca.Speech:/org/gnome/orca/default/speech/" speech-server-factory spiel` (values: `speechdispatcherfactory` | `spiel`; the `:path` suffix is mandatory). libspiel is still unpackaged on Ubuntu 26.04 -- source build + `~/.config/environment.d/` typelib path.

## Testing

No test suite. Validate changes by:
1. Restarting the service (`systemctl --user restart gnome-speaks.service`)
2. Checking logs (`journalctl --user -u gnome-speaks.service -f`)
3. Testing via D-Bus (`dbus-send`) or keyboard shortcuts
4. Python syntax check: `python3 -c "import py_compile; py_compile.compile('gnome-speaks-service.py', doraise=True)"`
5. Shell-side changes: headless session (`--nested` is dead on 50, see gotchas) --
   `dbus-run-session -- gnome-shell --headless --virtual-monitor 1280x720`, then
   enable/disable/re-enable and require **zero** JS errors, shell CRITICALs, and St warnings.
6. Wake-word secure gate: `python3 verify_wake_gate.py` from the checkout root -- no env vars,
   no desktop, no daemon (it imports `ibus_injector` from its own directory). 24 checks; exit 0
   means all passed.
7. Service-side changes: the de-facto verification bar is the scratch repro suites under
   `~/.claude/projects/-home-jp/scratch/gnome-speaks-dreamteam/`, one directory per audit --
   `lucid-service-audit-repros` (queue invariants, dispatch gate, config),
   `lucid-chronicle-perf-repros` (chronicle contract, archive, HTTP endpoints),
   `lucid-cancel-tokens-repros` (cancel-token verdicts, stop vs stop_listening),
   `morpheus-injector-seam-repros` (Injector seam, IbusInjector). They import the service by
   path and need no running service, D-Bus, mic or port 7710. Run the suite(s) covering the
   area you touched before opening a PR.

   **Always pass the path explicitly; never trust a suite's default.** Only
   `lucid-cancel-tokens-repros` defaults to the main checkout. `lucid-service-audit-repros`,
   `lucid-chronicle-perf-repros` and `morpheus-injector-seam-repros` default to
   `~/Projects/gnome-speaks-wt/<audit-name>/` worktrees that no longer exist, so an
   unqualified run dies with `FileNotFoundError` on that path instead of testing anything.

   ```bash
   SVC=~/Projects/gnome-speaks-wt/<your-worktree>/gnome-speaks-service.py
   cd ~/.claude/projects/-home-jp/scratch/gnome-speaks-dreamteam
   GS_SVC_PATH=$SVC       python3 lucid-service-audit-repros/verify_queue_invariants.py
   GS_SVC_PATH=$SVC       python3 lucid-chronicle-perf-repros/verify_archive.py
   GS_SVC_PATH=$SVC       python3 lucid-cancel-tokens-repros/verify_cancel_invariants.py
   GS_SVC_PATH=$SVC       python3 morpheus-injector-seam-repros/verify_injector_seam.py
   GS_WT=$(dirname $SVC)  python3 morpheus-injector-seam-repros/verify_ibus_injector.py
   ```

   `verify_ibus_injector.py` is the exception: it imports `ibus_injector` as a module rather
   than loading the service by path, so it reads **`GS_WT` (a checkout *directory*)** and
   ignores `GS_SVC_PATH` entirely -- passing only `GS_SVC_PATH` gets you
   `ModuleNotFoundError: No module named 'ibus_injector'`.

   Exit 0 = clean. A non-zero exit is a failed check -- in the `repro_*` scripts that means
   the bug the repro was written for is still present, which is the point of running them.
   **A red suite is not automatically your fault**: some scripts are open-bug repros that are
   red on `main` by design, and several keep state under `/tmp/<suite>/` that is not reset
   between runs. Get a baseline on `main` before blaming your branch, and wipe the suite's
   state dir if a second run disagrees with the first.

8. **prefs.js changes: the broadway rig.** Lives in the same scratch dir as the suites above,
   as `luna-prefs-async-repros/`. ⚠️ **`gnome-extensions prefs
   gnome-speaks@jphein` CANNOT verify a worktree** -- it goes through the live shell and opens
   the copy **installed** in `~/.local/share/gnome-shell/extensions`, so it renders the OLD
   prefs.js and reports success. Item 5 does not cover this either: gnome-shell never loads
   prefs.js at all (the prefs window is a separate process), so a clean headless shell says
   nothing about it.

   ```bash
   cd ~/.claude/projects/-home-jp/scratch/gnome-speaks-dreamteam/luna-prefs-async-repros
   ./run.sh ~/Projects/gnome-speaks-wt/<wt>/prefs.js                     # one file
   ./run.sh ~/Projects/gnome-speaks-wt/<wt>/prefs.js /path/to/main.js    # + baseline diff
   ```

   It builds a **real, mapped `Adw.PreferencesWindow`** on a `gtk4-broadwayd` virtual display,
   so **no window appears and no focus is stolen -- safe to run while JP is dictating**. `HOME`
   is sandboxed and `GSETTINGS_BACKEND=memory` is forced, so a run can never write
   `~/.config/speech-to-cli/config.json` or dconf. It reports per combo row: options and
   selected index **synchronously** and again **after async probes land**; `WRITES` (must be
   `none` -- swapping a `Gtk.StringList` moves `selected` and emits `notify::selected`, which
   `_addComboRow` treats as a user choice, so a careless async fill rewrites the setting it is
   still loading); and `BLANK_RENDERED`.

   ⭐ **`BLANK_RENDERED` exists because a property snapshot cannot see a markup failure.** When
   `set_markup` fails, the *property* keeps the string and the *label* is left EMPTY, so the
   bug is invisible both in the code and in any check that reads properties. It flags anything
   whose text is set but renders blank. On `main` before the markup fix it found 5 -- four shortcut rows
   whose subtitles are raw accelerators (`<Super><Alt>space` parses as an unclosed tag) and the
   `Privacy & Debug` group title.

   **Always pass a baseline.** A run that CRASHED also produces "no new warnings", so `run.sh`
   requires the harness to print `EXIT_CLEAN` before it believes any stderr comparison -- two
   crashes have identical stderr and would otherwise read as a pass.

   **Exit contract:** 0 = both runs completed; 1 = a run did not complete, or the branch emits
   MORE warnings than the baseline. A differing stderr is **not** failure -- a fix is supposed
   to change stderr, and keying the exit code on `diff` made a successful run report 1.

   **Isolation contract** -- the same shape the service-side suites enforce, and none of it is
   optional:
   - Everything a run writes lives under `run-$$` and is deleted on exit. **No fixed paths**,
     so two rigs running at once cannot share a sandbox, a schema dir, or a broadway socket
     (the display number is per-PID and probed until one binds). Verified by running two
     concurrently: distinct dirs, distinct displays, identical verdicts, no leftovers.
   - The `config.json` handed to prefs.js is a **rig-owned fixture with pinned keys**.
     `~/.config/speech-to-cli/config.json` is **never read** -- an earlier version copied it
     and let 47 of JP's live keys steer the verdict, and cached that copy forever behind an
     `if [ ! -f ]`. Device ids in the fixture come from live `wpctl`, so the "config names a
     device" path is exercised on any machine.
   - `HOME` and `GSETTINGS_BACKEND=memory` are exported into the gjs child only, never into
     the calling shell, so a run cannot write the real config or dconf.

## Git

Conventional commits (`feat:`, `fix:`, `refactor:`). Branch naming: `<type>/<short-description>`.
