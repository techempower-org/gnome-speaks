# GNOME Speaks

A GNOME Shell extension that adds voice interaction to your desktop — speech-to-text dictation and text-to-speech readback — powered by [Azure Speech Services](https://azure.microsoft.com/en-us/products/ai-services/speech-services).

## Ecosystem

GNOME Speaks is the desktop integration layer for a four-project voice AI system:

```
┌──────────────────────────────────────────────────────────────────┐
│                     GNOME Shell (Wayland)                         │
│  ┌─────────────┐                                                 │
│  │ extension.js │ ◄──── UI only: badge, panel menu, keybindings  │
│  └──────┬──────┘                                                 │
│         │ D-Bus (org.gnome.Speaks)                                │
│  ┌──────▼──────────────────┐                                     │
│  │ gnome-speaks-service.py │ ◄──── Orchestrator                  │
│  └──┬─────────┬────────────┘                                     │
│     │         │                                                  │
│  ┌──▼──┐  ┌──▼──────────────────┐   ┌──────────────────────┐    │
│  │ STT │  │ LLM (direct API or  │   │   the-oracle         │    │
│  │ TTS │  │ cloud-chat-assistant)│   │   (web UI + proxy)   │    │
│  └──┬──┘  └─────────────────────┘   └──────────┬───────────┘    │
│     │                                           │                │
│  ┌──▼────────────┐              ┌───────────────▼────────────┐   │
│  │ speech-to-cli  │              │ cloud-chat-assistant (MCP) │   │
│  │ (audio engine) │              │ Azure AI / Bedrock / Google│   │
│  └────────────────┘              └───────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

| Project | Role | Config file |
|---------|------|-------------|
| [speech-to-cli](https://github.com/techempower-org/speech-to-cli) | Audio engine — STT, TTS, VAD, recorder, WebSocket | `~/.config/speech-to-cli/config.json` |
| [cloud-chat-assistant](https://github.com/techempower-org/cloud-chat-assistant) | Multi-cloud LLM provider — Azure AI, Bedrock, Google | `~/.config/cloud-chat-assistant/config.json` |
| [the-oracle](https://github.com/techempower-org/the-oracle) | Web frontend — proxies both MCP servers through FastMCP | Reads both config files above |
| **gnome-speaks** (this project) | GNOME Shell integration — badge, panel, keybindings, modes | Both config files + GSettings |

GNOME Speaks preferences can configure all four projects from one unified settings panel.

## Features

- **Floating voice badge** — glassmorphism-styled status indicator with pulse animations
- **Panel menu** — quick access to all voice actions from the top bar
- **Speech-to-text** — real-time streaming transcription via Azure WebSocket STT
- **Live typing** — partial transcriptions appear in the text field as you speak, replaced by the final text when done
- **Text-to-speech** — HD and Fast voice modes (DragonHD / Neural) with streaming playback
- **Streaming LLM→TTS** — AI responses start speaking on the first complete sentence instead of waiting for the full reply
- **Continuous STT session** — in loop mode, the recorder and WebSocket stay alive across cycles (no restart overhead)
- **Voice quality toggle** — switch between HD (DragonHD, eastus) and Fast (Neural, westus) modes via `Super+Alt+V` or the panel menu
- **Keyboard shortcuts** — `Super+Alt+Space` (listen), `Super+Alt+C` (speak clipboard), `Super+Alt+R` (read selection), `Super+Alt+V` (toggle voice quality)
- **Dictation mode** — transcribed text is typed at the cursor position via ydotool (Wayland) or xdotool (X11)
- **Conversation mode** — voice-to-LLM-to-voice with support for Anthropic, OpenAI, Azure AI, Google Vertex, and AWS Bedrock
- **Continuous dictation** — keeps listening after each utterance
- **Voice commands** — spoken punctuation ("period", "comma", "new line") converted to characters
- **Auto-corrections** — custom find-and-replace rules applied to transcriptions
- **Language switching** — change STT/TTS language on the fly (15 languages)
- **Audio visualization** — badge scales with microphone input level
- **Service auto-reconnect** — badge resets automatically when the service restarts

## Architecture

```
GNOME Shell process                    Background service
┌─────────────────┐     D-Bus IPC     ┌──────────────────────┐
│  extension.js   │◄──────────────────►│ gnome-speaks-service │
│  (UI only)      │   org.gnome.Speaks │  (Python)            │
│  - badge        │                    │  - PipeWire capture  │
│  - panel menu   │                    │  - Azure STT (WS)    │
│  - keybindings  │                    │  - Azure TTS (REST)  │
│  - settings     │                    │  - ydotool live type │
│  - bus watcher  │                    │  - LLM integration   │
└─────────────────┘                    └──────────────────────┘
```

The extension runs inside GNOME Shell's process and handles only UI. All network calls, audio I/O, and speech processing happen in a separate Python service communicating over the session D-Bus. Live typing uses ydotool (or xdotool) to inject keystrokes via `/dev/uinput`, bypassing Wayland's input restrictions.

## Modes

GNOME Speaks has several modes that can be combined for different workflows:

### Type Mode (default)

Click the badge or press `Super+Alt+Space` → speak → text is typed at the cursor position. Click again, say "over", or pause for silence to stop. The transcription appears character-by-character as you speak (live typing via ydotool).

### AI Mode

Enable via the panel menu or preferences. Your speech is sent to an LLM (Claude, GPT, Gemini, etc.) and the response is spoken aloud. With streaming LLM→TTS, speech starts on the first complete sentence — you don't wait for the full reply.

### Continuous Dictation (Loop)

Enable via the panel menu. After each pause, listening automatically restarts. Works in both Type and AI modes:

- **Type + Loop**: Speak continuously — each utterance is typed, then listening restarts. The recorder and WebSocket session stay alive across cycles (no restart overhead).
- **AI + Loop (Hands-Free)**: Speak → AI responds → auto-listens again. Full voice assistant loop.

The `loop_silence_timeout` setting (default 1.2s) controls how quickly each cycle ends on silence — shorter values mean faster turnaround.

### Terminal Mode

All lowercase, no auto-capitalization or punctuation. Uses Azure's lexical output for code and terminal input. AI-generated terminal commands are pasted via clipboard (not ydotool) to avoid character drops.

### Talk Mode (D-Bus API)

A programmatic interface for external applications. An app calls `org.gnome.Speaks.Talk(text)` over D-Bus, which:

1. **Speaks** the provided text aloud (TTS)
2. **Listens** for the user's spoken reply (STT)
3. **Returns** the transcribed reply as a string

The D-Bus call blocks until the user responds. Used by Claude Code, Copilot CLI, and MCP servers to have voice conversations through GNOME Speaks. The `talk_silence_timeout` (default 4.0s) controls how long it waits for a reply.

### Half-Duplex vs Full-Duplex

This is an audio routing concern, not a mode:

- **Full duplex** (headphones): TTS plays while the recorder is already prewarmed. The mic won't pick up speaker output, so listening can start immediately.
- **Half duplex** (speakers): TTS must finish completely before the mic opens, otherwise it would transcribe the speaker output as speech. A 0.5s drain buffer is added after TTS ends.
- **Auto** (default): Detects whether audio is going to speakers or headphones and sets duplex mode accordingly.

### Notification Reader

Automatically reads GNOME desktop notifications aloud as they arrive.

## Requirements

- GNOME Shell 46, 47, or 48
- PipeWire (for audio capture and playback)
- Python 3.10+
- An [Azure Speech Services](https://azure.microsoft.com/en-us/products/ai-services/speech-services) API key

### Python dependencies

```
requests websocket-client webrtcvad numpy
```

### System tools

```
pw-record aplay glib-compile-schemas wl-paste ydotool
```

#### ydotool (recommended: v1.0+ from source)

The packaged version on Ubuntu 24.04 (v0.1.8) works but adds ~50ms latency per keystroke.
For instant live typing, build v1.0+ from source to get the `ydotoold` daemon:

```bash
sudo apt install -y cmake scdoc git build-essential
git clone https://github.com/ReimuNotMoe/ydotool.git /tmp/ydotool
cd /tmp/ydotool && mkdir build && cd build && cmake .. && make -j$(nproc)
sudo make install
sudo systemctl enable --now ydotool.service
```

The service auto-detects whether `ydotoold` is running and adjusts accordingly.

## Installation

### Quick install

```bash
git clone https://github.com/techempower-org/gnome-speaks.git
cd gnome-speaks
./install.sh
```

The installer will:
1. Copy extension files to `~/.local/share/gnome-shell/extensions/gnome-speaks@jphein/`
2. Compile GSettings schemas
3. Install and start the systemd user service
4. Register the D-Bus service for auto-activation
5. Install missing Python dependencies
6. Enable the extension

Restart GNOME Shell after installation (log out and back in on Wayland, or `Alt+F2` → `r` on X11).

### Meson build (alternative)

```bash
meson setup build
meson install -C build
```

### Uninstall

```bash
./install.sh --uninstall
```

## Configuration

### Azure Speech key

Create `~/.config/speech-to-cli/config.json`:

```json
{
    "key": "YOUR_AZURE_SPEECH_KEY",
    "region": "westus",
    "tts_region": "eastus",
    "voice": "en-US-Ava:DragonHDLatestNeural",
    "fast_voice": "en-US-AvaNeural",
    "language": "en-US"
}
```

| Key | Description |
|-----|-------------|
| `key` | Azure Speech API key (or set `AZURE_SPEECH_KEY` env var) |
| `region` | STT region and fast-voice TTS region (e.g., `westus`) |
| `tts_region` | HD voice TTS region (e.g., `eastus` — DragonHD voices are only available in select regions) |
| `voice` | HD voice name (used in HD quality mode) |
| `fast_voice` | Fast voice name (used in Fast quality mode) |
| `language` | STT/TTS language code |

You can get a free Azure Speech key at [Azure Portal](https://portal.azure.com) — the free tier includes 500K characters/month for TTS and 5 hours/month for STT.

### Extension preferences

Open GNOME Extensions app → GNOME Speaks → Preferences, or:

```bash
gnome-extensions prefs gnome-speaks@jphein
```

Settings include voice selection (HD/fast), silence timeout, keyboard shortcuts, conversation mode (LLM provider and model), auto-corrections, and badge positioning.

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `Super+Alt+Space` | Toggle listening (start/stop STT) |
| `Super+Alt+C` | Speak clipboard contents aloud |
| `Super+Alt+R` | Read selected text aloud |
| `Super+Alt+V` | Toggle voice quality (HD / Fast) |

All shortcuts are configurable in the extension preferences.

## Performance

The service is optimized for low-latency voice interaction:

- **Prewarmed connections** — recorder process, STT WebSocket, and TTS HTTP session are kept alive between uses
- **Continuous STT session** — in loop mode, the recorder and WebSocket stay alive across multiple utterances (no per-cycle restart)
- **Streaming LLM→TTS** — SSE token streaming with sentence boundary detection; TTS starts on the first complete sentence
- **Inline noise calibration** — audio frames are sent to Azure while calibrating (no blocking delay)
- **WebSocket reuse** — persistent STT connection saves ~230ms per utterance
- **Numpy RMS fast-path** — SIMD-vectorized audio energy calculation (~5-10x faster)
- **Diff-aware live typing** — only erases and retypes the changed suffix of each partial hypothesis
- **ydotool daemon mode** — with ydotoold, keystroke injection is sub-millisecond (no uinput device churn)
- **Loop silence timeout** — configurable 1.2s (vs 3.0s single-shot) for fast cycle turnaround

## Service management

```bash
# Check status
systemctl --user status gnome-speaks

# View live logs
journalctl --user -u gnome-speaks -f

# Restart after config changes
systemctl --user restart gnome-speaks
```

## HTTP API

A localhost REST API on port `7710` for browser- and agent-driven TTS. Speech
requests **queue FIFO** — concurrent callers never cut each other off — while
speech you trigger yourself (keyboard shortcut, D-Bus, AI replies) preempts the
current utterance immediately and holds the queue until you finish.

| Endpoint | Description |
|----------|-------------|
| `POST /speak` | Queue text for speech. Body: `text` (required), `voice` (Azure ShortName), `quality` (`fast`/`hd`), `output_file` (save WAV instead of playing), `interrupt` (`true` = flush everything and speak now). Returns `{ok, id, position, state}` — plus `flushed` (how many queued items an interrupt deleted); `429` when the queue (depth 32) is full. |
| `POST /skip` | Cancel the current utterance; the next queued one plays. Returns `{ok, skipped}`. (SSIP calls this `STOP`.) |
| `POST /stop` | Panic button: stop playback and drain the queue. Returns `{ok, cleared}`. (SSIP calls this `CANCEL` — note the inverted verbs if you have speech-dispatcher reflexes.) |
| `POST /pause` / `POST /resume` | Pause/resume. Queue-level: while paused, the next queued item won't start either. |
| `GET /queue` | Queue introspection: `{current, pending, depth, recent}`. `recent` holds the last 16 terminal outcomes — `done`, `canceled` (dropped before starting), `interrupted` (cut off mid-play), or `error` — so callers can learn the fate of a submitted `id`. |
| `GET /status` | Service state, pause flag, `queue_depth`, playback progress. |
| `GET /voices` | Available Azure voices (cached 5 min). |

```bash
curl -X POST localhost:7710/speak -H 'Content-Type: application/json' \
  -d '{"text": "Hello from the queue", "voice": "en-US-JennyNeural"}'
```

## Voice Spellbook

Utterances beginning with a trigger word (`cast` or `invoke`) are **incantations**:
they route to a local spellbook instead of being typed or sent to the LLM. Works in
every mode; a realm chime confirms the match instantly (~2 ms) and spoken replies
ride the speech queue (during continuous dictation, replies wait for the mic to
close — casting never speaks over an open microphone). An unknown incantation
speaks "The spell fizzles" instead of silently typing.

Spells are data: the repo ships `spellbook.json` with the self-control spells;
a user overlay at `~/.config/speech-to-cli/spellbook.json` merges over it by
spell name and hot-reloads on save. The realm/oracle/home spells below are
overlay examples — their endpoints are site-specific and never live in the repo.

| Incantation | Effect |
|---|---|
| "cast silence" / "cast skip" | stop everything / skip current utterance |
| "cast terminal mode" / "ai mode" / "type mode" | switch modes |
| "cast read notifications" | toggle the notification herald |
| "cast defense report" | combat-ward summary, spoken |
| "cast my level" | character sheet from realm progression |
| "cast recent events" | last 5 realm events, spoken |
| "cast mana reserves" | solar/battery/grid report |
| "cast consult the oracle \<question\>" | ask the realm Oracle; streamed reply |
| "cast torches \<request\>" | natural-language pass-through to Home Assistant Assist |

Safety: spells are gated `instant` (read-only/reversible) or `confirm` (the service
speaks a challenge and requires a spoken "confirm"); a hardcoded executor denylist
refuses to load any spell touching destructive surfaces (grid transfer, locks,
valves, safety automations, remote exec) regardless of config. Test or script
spells without a microphone via `POST /cast`:

```bash
curl -X POST localhost:7710/cast -H 'Content-Type: application/json' \
  -d '{"text": "cast defense report"}'
```

## License

[GPL-3.0-or-later](LICENSE)
