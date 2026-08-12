<!-- claude-md-version: c2e0cdd | updated: 2026-03-22 -->
# CLAUDE.md — gnome-speaks

GNOME Shell extension (v46-48) for desktop voice interaction: STT, TTS, and AI conversation via Azure Speech Services.

## Architecture

Two-process design connected by session D-Bus (`org.gnome.Speaks`):

| File | Runtime | Role | Lines |
|------|---------|------|-------|
| `extension.js` | GNOME Shell (GJS) | UI: badge, panel indicator, subtitle overlay, keybindings, drag | ~1,800 |
| `gnome-speaks-service.py` | systemd user service (Python) | Audio, STT, TTS, speech queue, LLM, typing, clipboard, wake watcher | ~3,400 |
| `spellbook.py` | imported by the service | Incantation matcher + executor ("cast …" → local actions); denylist | ~450 |
| `spellbook.json` | data | Repo spells (self-control); user overlay at `~/.config/speech-to-cli/spellbook.json` merges + hot-reloads | — |
| `prefs.js` | GNOME Extensions app (GJS/Gtk4) | 10-page preferences window (incl. Spellcraft) | ~1,750 |
| `stylesheet.css` | GNOME Shell | Badge states, animations, subtitle overlay | ~350 |

The extension touches **no** network or audio -- all I/O is in the Python service.
Spoken output serializes through a FIFO **speech queue** (HTTP callers never stomp
each other; user speech preempts). Transcripts hit the **spellbook** before mode
routing. A **wake watcher** thread streams mic audio to a LAN Wyoming
openwakeword server while idle.

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

## Service Management

```bash
systemctl --user status gnome-speaks.service
systemctl --user restart gnome-speaks.service
journalctl --user -u gnome-speaks.service -f    # live logs
```

HTTP REST API on `localhost:7710`: `POST /speak` (queues FIFO; `interrupt:true`
flushes), `/skip`, `/stop` (drains queue), `/pause`, `/resume`, `/cast` (text
seam into the spellbook — same gates as spoken casts), `GET /status`, `/queue`
(pending + per-item outcomes: done/canceled/interrupted/error), `/voices`.

## D-Bus Interface

Bus name: `org.gnome.Speaks` | Path: `/org/gnome/Speaks`

Key methods: `StartListening`, `StopListening`, `Speak(text)`, `SpeakClipboard`, `SpeakSelection`, `Talk(text)`, `Stop`, `GetState`

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

## Coding Conventions

- **extension.js**: GJS with GNOME Shell imports (St, Clutter, Meta, Shell). No ES modules from npm -- pure GObject Introspection. Prefix private methods with `_`.
- **gnome-speaks-service.py**: GLib main loop + threading for blocking audio/network ops. `GLib.idle_add()` to marshal D-Bus signal emissions back to the main thread. Logs to stderr via `logging`.
- **prefs.js**: Adw (libadwaita) preferences pages. Config changes written to `~/.config/speech-to-cli/config.json` with debounced saves.
- **stylesheet.css**: GNOME Shell CSS (subset of CSS3). No SCSS or preprocessors.

## Key Gotchas

- **ydotool stuck keys**: If a ydotool command is interrupted between key-down and key-up, the virtual device retains that key as pressed. The service auto-restarts `ydotoold` to recover. Scripts: `fix-ydotool.sh`, `install-ydotool.sh`.
- **pw-record ignores SIGTERM**: Must use SIGKILL (`proc.kill()`) to stop PipeWire recorder processes.
- **Half-duplex drain**: On speakers, 0.5s delay after TTS before opening mic to prevent echo pickup.
- **Config dual-write**: Mode flags exist in both the Python `CONFIG` dict (runtime) and `~/.config/speech-to-cli/config.json` (disk). `_reload_config_flags()` and `_save_config_flag()` keep them in sync. Be careful not to create drift.
- **Schema compilation**: After editing the `.gschema.xml`, must run `glib-compile-schemas` on the install directory.
- **Disposed notification sources**: During shell init/restart, `MessageTray` `source-added` can fire with already-disposed `FdoNotificationDaemonSource` objects. Any signal connection on them crashes the shell. Always wrap `source.connect()` in try-catch and listen for `source-removed` to drop references before GC disposes them.
- **Azure content filter**: Avoid `[SYSTEM:]` prefix in system prompts -- Azure GPT content filter blocks it.
- **speech-to-cli `load_config()` whitelists keys**: unknown config.json keys are silently dropped. Adding a config key means adding it to the whitelist in `state.py` too, or the feature reads a default forever.
- **`Shell.Eval` is dead** (returns `(false,'')`; Introspect/Screenshot are AccessDenied) -- `_get_focused_app()` is silently a no-op (#7). Desktop actuation needs D-Bus methods exported from extension.js.
- **Public repo**: LAN hostnames/IPs, the HA domain, and the wake-word model name (it's the wake phrase) never enter git -- they live in `~/.config/speech-to-cli/config.json` and the user spellbook overlay. Scan patch history before pushing.
- **systemctl scope trap**: this file prescribes `systemctl --user` for the voice service — but `systemctl --user is-active <system-unit>` answers `inactive` with **exit 0** for units that live in the system scope (e.g. litrpg-engine on this machine). A confidently wrong answer; check the scope before believing "inactive", and never build a health check or spell on the --user reading of a system unit.
- **Speech-queue state ownership**: `_speak_token` fences playback cleanup -- a preempted worker must not reset state it no longer owns. Keep the token claims when adding new speech paths.

## Testing

No test suite. Validate changes by:
1. Restarting the service (`systemctl --user restart gnome-speaks.service`)
2. Checking logs (`journalctl --user -u gnome-speaks.service -f`)
3. Testing via D-Bus (`dbus-send`) or keyboard shortcuts
4. Python syntax check: `python3 -c "import py_compile; py_compile.compile('gnome-speaks-service.py', doraise=True)"`

## Git

Conventional commits (`feat:`, `fix:`, `refactor:`). Branch naming: `<type>/<short-description>`.
