<!-- claude-md-version: c2e0cdd | updated: 2026-03-22 -->
# CLAUDE.md — gnome-speaks

GNOME Shell extension (v46-50) for desktop voice interaction: STT, TTS, and AI conversation via Azure Speech Services.

## Architecture

Two-process design connected by session D-Bus (`org.gnome.Speaks`):

| File | Runtime | Role | Lines |
|------|---------|------|-------|
| `extension.js` | GNOME Shell (GJS) | UI: badge + pills + 📜 rune, panel indicator, subtitle overlay, chronicle scroll, keybindings, drag | ~2,620 |
| `gnome-speaks-service.py` | systemd user service (Python) | Audio, STT, TTS, speech queue, chronicle, LLM, typing, clipboard, wake watcher | ~3,720 |
| `spellbook.py` | imported by the service | Incantation matcher + executor ("cast …" → local actions); denylist | ~390 |
| `spellbook.json` | data | 12 repo spells (self-control); user overlay at `~/.config/speech-to-cli/spellbook.json` merges + hot-reloads | — |
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
| Wake word | Idle-only mic stream to LAN openwakeword; detection = dictation hotkey. Toggle: "cast wake word" |
| Spellbook | "cast …"/"invoke …" transcripts run local spells (never typed/LLM'd); `POST /cast` is the text seam |
| Chronicle | Not a mode -- always-on ledger of both directions; 📜 badge rune (8 lines) + panel submenu (12), click to respeak. Spells: "cast echo" / "chronicle" / "seal the chronicle" |

## Coding Conventions

- **extension.js**: GJS with GNOME Shell imports (St, Clutter, Meta, Shell). No ES modules from npm -- pure GObject Introspection. Prefix private methods with `_`.
- **gnome-speaks-service.py**: GLib main loop + threading for blocking audio/network ops. `GLib.idle_add()` to marshal D-Bus signal emissions back to the main thread. Logs to stderr via `logging`.
- **prefs.js**: Adw (libadwaita) preferences pages. Config changes written to `~/.config/speech-to-cli/config.json` with debounced, **merge-on-write** saves (#16): re-read the file and apply only the dirty/deleted keys, because the service writes the same file (spells, quality toggle) and dumping a stale full object would erase its changes. Track edits through `_setConfigValue`/`_deleteConfigKey` so they land in `_dirtyKeys`/`_deletedKeys` -- mutating `this._config` directly means the change is silently dropped at save time.
- **stylesheet.css**: GNOME Shell CSS (subset of CSS3). No SCSS or preprocessors.

## Key Gotchas

- **ydotool stuck keys**: If a ydotool command is interrupted between key-down and key-up, the virtual device retains that key as pressed. The service auto-restarts `ydotoold` to recover. Scripts: `fix-ydotool.sh`, `install-ydotool.sh`.
- **pw-record ignores SIGTERM**: Must use SIGKILL (`proc.kill()`) to stop PipeWire recorder processes.
- **Half-duplex drain**: On speakers, 0.5s delay after TTS before opening mic to prevent echo pickup.
- **Config dual-write**: Mode flags exist in both the Python `CONFIG` dict (runtime) and `~/.config/speech-to-cli/config.json` (disk). `_reload_config_flags()` and `_save_config_flag()` keep them in sync. Be careful not to create drift.
- **Schema compilation**: After editing the `.gschema.xml`, must run `glib-compile-schemas` on the install directory.
- **Disposed notification sources**: During shell init/restart, `MessageTray` `source-added` can fire with already-disposed `FdoNotificationDaemonSource` objects. Any signal connection on them crashes the shell. Always wrap `source.connect()` in try-catch and listen for `source-removed` to drop references before GC disposes them.
- **Azure content filter**: Avoid `[SYSTEM:]` prefix in system prompts -- Azure GPT content filter blocks it.
- **speech-to-cli `load_config()` whitelists keys**: unknown config.json keys are silently dropped. Adding a config key means adding it to the whitelist in `state.py` too, or the feature reads a default forever. Scope: this only binds keys the **Python** side reads -- keys consumed only by extension.js (`subtitles_user`, `subtitles_tts`) bypass it entirely, since GJS parses config.json raw. Don't "fix" their absence from `state.py`, and don't assume a working extension key means the Python side can see it.
- **`Shell.Eval` is dead** (returns `(false,'')`; Introspect/Screenshot are AccessDenied) -- `_get_focused_app()` is silently a no-op (#7). Desktop actuation needs D-Bus methods exported from extension.js.
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

## Git

Conventional commits (`feat:`, `fix:`, `refactor:`). Branch naming: `<type>/<short-description>`.
