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
| `spellbook.json` | data | 15 repo spells (self-control); user overlay at `~/.config/speech-to-cli/spellbook.json` (seam: `spellbook.USER_SPELLBOOK_PATH` / env `GS_SPELLBOOK_USER_PATH`) merges + hot-reloads | — |
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
| Loop | Auto-restart listening after each utterance. A badge tap while listening ENDS the utterance (text kept), stops the loop for this run and goes idle (#110, JP's choice (a)); the Loop pill / "cast loop" turns the mode off. Every restart guard checks `_stop_event`, and the batch (VAD) recorder honours it via `stt(stop_when=…)` -- before that a tap in offline/loop mode did nothing until VAD silence or 30 s |
| Terminal | Lowercase, no punctuation, lexical output |
| Talk | D-Bus API for external apps (blocking call) |
| Half/Full Duplex | Auto-detected speaker vs headphone routing |
| Wake word | Idle-only mic stream to LAN openwakeword; detection = dictation hotkey. Toggle: "cast wake word". Opt-in `wake_word_secure_gate` (prefs: "Only Type Into Known Fields"): a wake-opened session types only into a focused field the app has **not** declared PASSWORD/PIN — ONE verdict per session, taken where `live_typing` is computed (gating just the final paste missed live partials and Keep-Live-Text, #55); `start_listening(quick=True)` keeps the wake mark. It **fails open**: ibus-daemon 1.5.34 always forwards `SetContentType`, and an undeclared field arrives as `(0,0)` = FREE_FORM, so an undeclared password box is undetectable. Works on native Wayland (measured GNOME 50) — it is not X11-only. `python3 verify_wake_gate.py` checks it |
| Speech backend | `speech_backend`: `azure` (default) or `local`. Local = the Wyoming server (Piper TTS / Parakeet STT) is PRIMARY and Azure is the fallback: a Wyoming failure trips a 60 s *local* breaker (`wyoming.mark_local_down`) and that session uses Azure; Azure failures trip the existing Azure breaker the other way. `SPEECH_FORCE_OFFLINE=1` still forces offline with NO Azure fallback -- it is an env override for tests, not a setting; a leftover drop-in forced JP offline for weeks (#104). `wyoming.skip_reason()` names the route (`forced` / `prefer_local` / `azure_down`); the service logs it and `GET /status` carries it under `speech` |
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
  the live set forever. `CancelRegistry` is now the **only** place in the service that touches the
  wire — #42 took the last two reads out of `_run_subtitle_progress` — so
  `grep -n "_cancel_event" gnome-speaks-service.py` returning only that class and prose is a cheap
  standing check that no new verdict is being read off the wire.
- **The batch (VAD) recorder only ever watched the CANCEL wire**: `stop_listening()` deliberately leaves the wire
  alone (it would kill the WS), so in vad mode a stop had no effect until VAD silence or `max_seconds` -- the
  "stuck listening" of 2026-09-07 (#110). speech-to-cli's `stt(stop_when=…)` / `record_with_vad(stop_when=…)` is
  the FINISH-EARLY-AND-KEEP predicate; the service passes `self._stop_event.is_set`. `_STT_HAS_STOP_WHEN` guards
  an older speech-to-cli. Fixed-length recording (`stt_fixed`) still cannot be cut short (WAV header).
- **`stop()` and `stop_listening()` both set `_stop_event` and mean opposite things**:
  `stop_listening()` is the dictation hotkey — end the utterance, **keep** the text.  `stop()` is the
  panic stop (D-Bus Stop, `POST /stop`, "cast stop", every user-speech preemption) — **abandon** it.
  `_stop_event` cannot express the difference and is cleared by the next `start_listening()`, so
  only `stop()` cancels the session token and only the token may gate the transcript. A library
  result is not a cancellation check either: `stt_fixed()` calls `is_cancelled()` exactly once and
  then POSTs to Azure with a 30 s timeout, so a stop during the upload is simply never seen.
- **IBus's `fake` client is "no text field", not a target**: ibus-daemon focuses its pseudo-client `fake` when NO
  input context has focus -- which is exactly what a pointer tap on badge chrome produces (the tap gives the shell
  stage key focus; the window's context loses IBus focus). A `commit_text` into it vanishes: on 2026-09-07 a
  recognised transcript was lost that way with no toast (#109). `acquire()` therefore treats `client == "fake"` as a
  refusal with reason `no_target`, `commit()` types that utterance through the ydotool fallback (never for
  `secure` -- a password field gets nothing from any backend, including after a mid-session focus move), and the
  extension hands stage key focus back to the window before acting on a pointer tap (`_releaseKeyFocusFromPointer`).
  Keyboard activation keeps focus (a11y).
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

- **A pinned injector must also be RELEASED**: the STT cycle pins `inj = get_injector()` so a
  mid-utterance "cast typing engine" cannot retract with the wrong backend (#46) -- but the pin
  is only half of it. `get_injector()` rebuilds on the config flip and merely `cancel()`s the
  outgoing backend, and the spell thread triggers that rebuild immediately (it reads
  `get_injector().name` for its reply). The cycle then keeps typing through the pin, and every
  text path runs `_ensure_session()` -> `acquire()`, so the *cancelled* backend RE-ACQUIRES --
  and it is no longer the process-wide `_injector`, so no later rebuild will ever cancel it
  again. The idle hook (`_set_state('idle')` -> `get_injector().end()`) ends the CURRENT
  backend, which is now a different object. Result: a stranded input method until
  `SESSION_MAX_SECONDS = 120`. So `end()` the outgoing backend at the re-pin, and the pinned one
  in the post-loop cleanup.
  ⚠️ Release once per SESSION, not per cycle -- `inj.end()` every cycle forces a restore plus a
  0.4 s `FOCUS_WAIT` re-acquire on every loop utterance.
  ⚠️ The release must stay BELOW every path that delivers text -- the final
  `finalize`/`paste`/`commit`, and the latched dead-recorder verdict, which delivers through that
  same path before it reports. `end()` FLUSHES the coalescer and `cancel()` DISCARDS it, so
  releasing with the wrong one, or above the delivery, truncates the utterance the report is
  promising it kept.
  ⚠️ Anything the cycle hands the backend to must be given the PIN, not `get_injector()` --
  `_LiveTyper` types off the WS receive thread and resolved its own backend per hypothesis.
  ⚠️ And touch a backend's attributes through `getattr(..., default)` on this path. `name` has a
  default on the `Injector` base exactly so a partial backend degrades instead of crashing; an
  unguarded `inj.name` in the swap's *log line* raised AttributeError out of the whole STT cycle
  -- nothing recognized, nothing typed, on the ordinary healthy-Azure path.

- **`connection.register_object` leaks, `GLib.unix_signal_add` warns -- use the shims (#114)**: Python's
  `register_object` shadows `g_dbus_connection_register_object_with_closures`, which GLib 2.84 deprecated
  because the closure is handed an owned `GDBusMethodInvocation` nothing releases -- measured ~1.5 kB per D-Bus
  method call on PyGObject 3.56 / GLib 2.88, flat through `register_object_with_closures2`. Go through
  `_register_dbus_object()` and `_unix_signal_add` (GLibUnix when present, old API on GNOME 46-48 hosts); a
  bare call reintroduces both the leak and the startup deprecation lines, and `tests/repros/deprecations`
  goes red -- it runs the real `main()` on a private bus and counts warnings attributed to the service file.
- **ydotool stuck keys**: If a ydotool command is interrupted between key-down and key-up, the virtual device retains that key as pressed. The service auto-restarts `ydotoold` to recover. Scripts: `fix-ydotool.sh`, `install-ydotool.sh`.
- **pw-record ignores SIGTERM**: Must use SIGKILL (`proc.kill()`) to stop PipeWire recorder processes.
- **The wake watcher's shutdown warning is the OLD pid, not the new one (#137)**: at `Stopping`, systemd's
  control-group SIGTERM reaches the watcher's `pw-record`, which exits rc=1 within ms (so the gotcha above is
  not universal -- measured on 5/5 restarts of 2026-09-07; why `proc.terminate()` is ignored while systemd's
  SIGTERM is not has NOT been established). `wyoming.detect_stream` then waits its 0.5 s for a verdict, and the
  watcher -- still alive while the main thread tears down -- logged `recorder produced no audio (rc=1)
  (retrying every 10s)` exactly 0.501 s into every stop, ~1 s before the next instance's `Starting` line.
  #137 read those as the NEW pid failing at start; three days of journal hold zero start-time recorder
  failures. **Read the pid before attributing a journal line to a start.** The watcher now returns on
  `_shutting_down` (set first in `shutdown()` and `_on_signal`), and failed cycles retry on a bounded ramp
  (0.5, 1, 2, 4, 8 s, DEBUG) before the unchanged #41 steady cadence (10 s recorder / 60 s server, WARNING).
  The real start-up window is at LOGIN: the LAN name may not resolve yet (measured 09:20:20.383, 15 ms after
  `Starting`), which used to be a flat 60 s of dead wake word. `tests/repros/wake-watcher` G/H cover both.
- **Half-duplex drain**: On speakers, 0.5s delay after TTS before opening mic to prevent echo pickup.
- **Config dual-write**: Mode flags exist in both the Python `CONFIG` dict (runtime) and `~/.config/speech-to-cli/config.json` (disk). `_reload_config_flags()` and `_save_config_flag()` keep them in sync. Be careful not to create drift.
- **Schema compilation**: After editing the `.gschema.xml`, must run `glib-compile-schemas` on the install directory.
- **Disposed notification sources**: During shell init/restart, `MessageTray` `source-added` can fire with already-disposed `FdoNotificationDaemonSource` objects. Any signal connection on them crashes the shell. Always wrap `source.connect()` in try-catch and listen for `source-removed` to drop references before GC disposes them.
- **Azure content filter**: Avoid `[SYSTEM:]` prefix in system prompts -- Azure GPT content filter blocks it.
- **`speech_backend` is a STRING flag in `_SYNC_FLAGS`**: the sync list was boolean-only; a string key there is
  applied verbatim (no bool cast) -- see `_reload_config_flags`. Do not add a bool coercion to that loop.
- **speech-to-cli `load_config()` whitelists keys**: unknown config.json keys are silently dropped. Adding a config key means adding it to the whitelist in `state.py` too, or the feature reads a default forever. Scope: this only binds keys the **Python** side reads -- keys consumed only by extension.js (`subtitles_user`, `subtitles_tts`) bypass it entirely, since GJS parses config.json raw. Don't "fix" their absence from `state.py`, and don't assume a working extension key means the Python side can see it. `tests/repros/config-keys` enforces the contract statically (prefs keys ⊆ whitelist ∪ `_SYNC_FLAGS` ∪ extension reads; `_SYNC_FLAGS` ⊆ whitelist; service `CONFIG` reads ⊆ prefs rows; every `_SYNC_FLAGS` key has a Python reader) -- it reads `state.py` from the sibling checkout, so a whitelist gap goes red here (#120, #127).
- **`Shell.Eval` is dead** (returns `(false,'')`; Introspect/Screenshot are AccessDenied) -- the service cannot ask the compositor anything directly (#7). The replacement is the `org.gnome.Speaks.Desktop` interface **exported by extension.js** (`GetFocusedApp` → `wm_class, title`), which `_get_focused_app()` calls via `gdbus`; it works only while the extension is loaded and returns `None` headless. Any further desktop actuation goes the same way: a method on that interface, answered from inside the Shell.
- **Public repo**: LAN hostnames/IPs, the HA domain, and the wake-word model name (it's the wake phrase) never enter git -- they live in `~/.config/speech-to-cli/config.json` and the user spellbook overlay. Scan patch history before pushing.
- **systemctl scope trap**: this file prescribes `systemctl --user` for the voice service — but `systemctl --user is-active <system-unit>` answers `inactive` with **exit 0** for units that live in the system scope (e.g. litrpg-engine on this machine). A confidently wrong answer; check the scope before believing "inactive", and never build a health check or spell on the --user reading of a system unit.
- **Speech-queue state ownership**: `_speak_token` fences playback cleanup -- a preempted worker must not reset state it no longer owns. Keep the token claims when adding new speech paths.
- **An agent seam never calls `stop()`**: `stop()` is `cancel_all()` + `_stop_event.set()` + idle -- every live
  token, the user's dictation included. `POST /speak {interrupt:true}` used to call it, then gated it on a
  snapshot of `current_state`; a `start_listening()` landing between the read and `cancel_all()` still lost its
  session (measured 43/126 in `tests/repros/cancel-tokens/repro_f_interrupt_race.py`). The interrupt path now
  cancels exactly the queue's current item via `skip_current()` (item + token read together under
  `_queue_current_lock`; the dispatcher only publishes tokens it issued), and `held` in the response is an
  annotation, not a decision. Any new agent-facing "cut off speech" path must do the same -- reach for
  `skip_current()`/`_drain_tts_queue()`, never `stop()`.
- **St CSS: measure, don't reason.** Specificity arithmetic on paper produced two wrong (and confidently shipped) conclusions in one day (2026-08-18): pill text was believed white (it was state-tinted by later type selectors) and a "(0,2,1)" counter-rule was really (0,1,1) and inert in 4 of 5 states. The instrument that works: dump computed `St.ThemeNode` values (foreground color, margins) from a headless shell per state and diff before/after. Badge labels are addressed by NAME (`gnome-speaks-badge-label`, `gnome-speaks-pill-label`); never reintroduce `StLabel` type selectors -- pill text tints by INHERITANCE from its pill class.
- **`--nested` is gone on GNOME 50**: the nested-shell test harness is now `dbus-run-session -- gnome-shell --headless --virtual-monitor 1280x720`. Any doc, script, or muscle memory reaching for `--nested` fails on 50+.
- **`addTopChrome` and `affectsInputRegion`**: GNOME 49+ tracks input regions from reactive actors automatically and **rejects** the param. It defaulted to `true` on 46-48, so omitting it is behavior-identical everywhere -- never re-add it.
- **St renders only ONE box-shadow**: comma-separated shadow lists log `Ignoring excess values` per rule and the extra layers never draw. Keep one shadow per rule and get depth from gradients instead.
- **`log()` is deprecated in GJS**: use `console.log/warn/error/debug`. Service-absent paths should log at `debug` so a solo-extension install (EGO users with no service) stays quiet.
- **`PopupSwitchMenuItem.setToggleState()` FIRES `toggled` on GNOME 50** (it is `this.set({state})`, and the
  item forwards its switch's `notify::state` as `toggled`), so a programmatic sync runs the same handler a
  click does. Syncing a menu switch from the D-Bus reply of the toggle it mirrors is an infinite loop at
  round-trip speed (#98: conversation mode flipped every ~5 ms, the badge flew offscreen). Every
  programmatic switch write goes through `_setSwitchQuietly()`, and every `toggled` handler returns early
  while `_quietSwitch` is set -- never call `setToggleState` directly.
- **`PopupSubMenu.open()` refuses an EMPTY submenu** (`popupMenu.js` guards on `isEmpty()`): populating a submenu from its own `open-state-changed` deadlocks -- the event never fires, the row is dead. Seed a placeholder at build time and refresh from the PARENT menu's open instead (the Chronicle submenu bug, cff6745).
- **Subtitles are conversation-mode only**: both user-voice subtitle paths early-return on `!this._conversationMode` (in dictation the text is already at the cursor). `subtitles_user` / `subtitles_tts` gate the two directions independently *on top of* the `live_subtitles` master; `live_subtitles` is dual-written to GSettings `live-subtitles` because the overlay gates on the GSettings layer.
- **Orca's Spiel switch moved**: `orca.settings.speechSystemOverride` **no longer exists** in Orca 50 -- an `orca-customizations.py` setting it does nothing silently. Use the relocatable GSettings schema: `gsettings set "org.gnome.Orca.Speech:/org/gnome/orca/default/speech/" speech-server-factory spiel` (values: `speechdispatcherfactory` | `spiel`; the `:path` suffix is mandatory). libspiel is still unpackaged on Ubuntu 26.04 -- source build + `~/.config/environment.d/` typelib path.

## Testing

No unit-test framework, by choice -- the repro suites in `tests/repros/` are plain
scripts that exit 0 or 1. Validate changes by:

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
7. **Service-side changes: `tests/repros/run_all.sh` is the verification bar.**

```sh
tests/repros/run_all.sh                       # the service in this repo
tests/repros/run_all.sh /path/to/service.py   # a worktree, or an extracted SHA
```

Run it before opening a PR that touches `gnome-speaks-service.py`. It needs no
running service, no D-Bus, no microphone and no port 7710; it exits non-zero if
any suite fails. `GS_SVC_PATH` is the only input and the runner sets it -- the
worktree dir and the scratch dir are derived, so there is nothing to pass by
hand and no default that can silently point at a tree that no longer exists.
Per-suite detail, the pinned baseline SHA each suite discriminates against, and
why `version-cache` is expected red are in `tests/repros/README.md`.

Two hazards these suites are built to avoid, both measured on 2026-09-06 --
keep them in mind before adding a suite, and read `tests/repros/README.md`
before changing one:

- **Never a fixed scratch path.** Suites shared absolute state dirs, and two
  agents running one suite at once corrupted each other; the failure looked
  exactly like a service regression and nearly got a good commit reverted.
  Scratch is per-PID, and reset/cleanup only ever touch a directory the running
  process created.
- **Never JP's live spellbook overlay** (#152). The service merges
  `~/.config/speech-to-cli/spellbook.json` over the repo spells at construction,
  so a harness-built service carried the personal spells (24 loaded, 15 repo).
  The overlay path is a seam — `spellbook.USER_SPELLBOOK_PATH`, env
  `GS_SPELLBOOK_USER_PATH` — never a literal in the service; `isolation.py`
  pins it to an empty scratch overlay and every `make_service()` asserts the
  loaded count equals the repo `spellbook.json`.
- **Never JP's live config.** `state.load_config()` reads
  `~/.config/speech-to-cli/config.json` at import, and `_reload_config_flags()`
  re-reads it *mid-run* for every `_SYNC_FLAGS` key -- so pinning a key after
  `load()` is undone at the next `start_listening()`. The harnesses rebuild
  `CONFIG` from the service's own defaults and repoint `CONFIG_PATH` at a
  scratch file, then assert that `CHRONICLE_PATH`/`CONFIG_PATH`/`XDG_STATE_HOME`
  all resolve inside it.

8. **prefs.js changes: the broadway rig.** Lives with the other suites in the repo,
   as `tests/repros/prefs-rig/` (GJS/bash, not python -- it is never
   collected by `run_all.sh`'s python path; the runner invokes its `run.sh`). ⚠️ **`gnome-extensions prefs
   gnome-speaks@jphein` CANNOT verify a worktree** -- it goes through the live shell and opens
   the copy **installed** in `~/.local/share/gnome-shell/extensions`, so it renders the OLD
   prefs.js and reports success. Item 5 does not cover this either: gnome-shell never loads
   prefs.js at all (the prefs window is a separate process), so a clean headless shell says
   nothing about it.

   ```bash
   cd tests/repros/prefs-rig
   # Paths must be ABSOLUTE: harness.js imports the file as a module URI, and a
   # relative path resolves against the rig dir, not your shell -- it fails with
   # `ImportError: Unable to load file async from: file://../prefs.js`.
   ./run.sh /abs/path/to/prefs.js                          # one file
   # + baseline diff. The baseline is the branch's MERGE BASE, never a moving
   # origin/main. Extract it to a real temp dir -- a placeholder in a `>`
   # redirect is a footgun, and the block above has already cd'd into the rig:
   BASE=$(mktemp -d)
   git show $(git merge-base origin/main HEAD):prefs.js > $BASE/prefs.js
   ./run.sh /abs/path/to/prefs.js $BASE/prefs.js
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
