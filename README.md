# GNOME Speaks

A GNOME Shell extension that adds voice interaction to your desktop — speech-to-text dictation, text-to-speech readback, hands-free wake-word activation, and a spoken command "spellbook" — powered by [Azure Speech Services](https://azure.microsoft.com/en-us/products/ai-services/speech-services), with automatic offline fallback to a LAN [Wyoming](https://github.com/rhasspy/wyoming) server (Piper + local STT) when the cloud is unreachable.

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

- **Floating voice badge** — glassmorphism-styled status indicator with pulse animations, mode pills, and a 📜 Chronicle rune
- **The Chronicle** — an append-only local ledger of everything said, both directions; click any line to hear it again, or say "cast echo"
- **Wake word** — hands-free activation via a LAN openwakeword server; speaking your wake phrase opens the mic like the keyboard shortcut (idle-only, never over TTS)
- **Voice spellbook** — "cast …" utterances run local spells instead of being typed: mode switching, status reports spoken back in themed voices, home-automation rituals, oracle consultations — with confirmation gates and a hardcoded denylist for anything destructive
- **Speech queue** — the HTTP API queues utterances FIFO so concurrent callers (agents, scripts, browsers) never cut each other off; your own speech always preempts
- **Offline fallback** — network-class Azure failures automatically reroute STT/TTS to a Wyoming server on your LAN (Piper voice, local transcription) behind a circuit breaker
- **Panel menu** — quick access to all voice actions from the top bar
- **Speech-to-text** — real-time streaming transcription via Azure WebSocket STT
- **Live typing** — partial transcriptions appear in the text field as you speak, replaced by the final text when done
- **Text-to-speech** — HD and Fast voice modes (DragonHD / Neural) with streaming playback
- **Prosody controls** — speaking rate, pitch, and volume applied as SSML to every utterance; the rate composes with Fast mode's built-in boost instead of being ignored by it
- **Per-voice subtitles** — the live-caption overlay has independent switches (and colors) for your voice and its voice
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
┌─────────────────┐     D-Bus IPC     ┌────────────────────────────┐
│  extension.js   │◄──────────────────►│ gnome-speaks-service.py    │
│  (UI only)      │   org.gnome.Speaks │  - PipeWire capture        │
│  - badge        │                    │  - Azure STT (WS) + TTS    │
│  - panel menu   │                    │  - speech queue + HTTP API │
│  - keybindings  │                    │  - spellbook.py dispatch   │
│  - subtitles    │                    │  - chronicle ledger        │
│  - chronicle UI │                    │  - wake-word watcher       │
│  - bus watcher  │                    │  - ydotool live typing     │
└─────────────────┘                    │  - LLM integration         │
                                       └──────────┬─────────────────┘
                                                  │ Wyoming (LAN, optional)
                                       ┌──────────▼─────────────────┐
                                       │ openwakeword · Piper · STT │
                                       └────────────────────────────┘
```

The extension runs inside GNOME Shell's process and handles only UI. All network calls, audio I/O, and speech processing happen in a separate Python service communicating over the session D-Bus. Live typing uses ydotool (or xdotool) to inject keystrokes via `/dev/uinput`, bypassing Wayland's input restrictions. Spoken output is serialized through a FIFO speech queue (see [HTTP API](#http-api)); transcripts pass through the [spellbook](#voice-spellbook) before mode routing; and an optional LAN Wyoming stack provides the wake word and offline STT/TTS.

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

- GNOME Shell 46–50 (developed on 46–48; verified on 50.1 / Ubuntu 26.04 LTS)
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
4. Register the D-Bus service for auto-activation (plus the inert [Spiel provider](#spiel-provider) name)
5. Install missing Python dependencies
6. Enable the extension

Dependencies are checked by **import**, not by `pip show` — distro packages and pip
metadata lie in opposite directions, and only the import is ground truth. On PEP 668
distros (Ubuntu 23.04+) the installer retries with `pip install --user
--break-system-packages`, which stays scoped to your user site-packages.

Restart GNOME Shell after installation (log out and back in on Wayland, or `Alt+F2` → `r` on X11).

### Extension-only zip (extensions.gnome.org)

`./pack.sh` wraps the official packer and writes
`dist/gnome-speaks@jphein.shell-extension.zip`. Only the shell extension ships in
that zip — the companion service (audio, STT/TTS, LLM, HTTP API) is not a shell
component and still installs via `install.sh`.

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

### Prosody

Three keys shape every spoken utterance, applied as SSML `<prosody>`:

| Key | Default | Values |
|-----|---------|--------|
| `speed` | `1.0` | Rate multiplier — `1.25` is 25% faster |
| `pitch` | `"default"` | `x-low`, `low`, `medium`, `high`, `x-high` |
| `volume` | `"default"` | `silent`, `x-soft`, `soft`, `medium`, `loud`, `x-loud` |

Those are the steps preferences offers; `pitch`/`volume` are passed through to SSML after an
injection-safety check, so relative forms Azure accepts (`+10%`, `-2st`) work if you edit the
config by hand.

`speed` **composes** with Fast quality's built-in +15% rather than being overwritten by it,
so the setting works in both quality modes; `speed: 1.0` still means "whatever the voice
does naturally". `pitch` and `volume` are left out of the SSML entirely while `"default"`,
so an untouched config produces the same markup it always did.

### Subtitles

The caption overlay has a master switch plus one switch per direction, because the two
directions are useful independently — captions of *its* voice with your own live transcript
off is a common preference:

| Key | Default | Meaning |
|-----|---------|---------|
| `live_subtitles` | `true` | Master switch (mirrored into GSettings `live-subtitles`) |
| `subtitles_user` | `true` | Live transcript while **you** speak |
| `subtitles_tts` | `true` | Captions while **it** speaks |
| `subtitle_color_user` / `subtitle_color_tts` | `light_green` / `amber` | Per-direction color |
| `show_word_highlights` | `true` | Newly heard words flash as they arrive |

Subtitles are a conversation-mode surface: in dictation mode the text is already landing at
your cursor, so the overlay stays out of the way.

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
| `POST /speak` | Queue text for speech. Body: `text` (required), `voice` (Azure ShortName), `quality` (`fast`/`hd`), `output_file` (save WAV instead of playing), `interrupt` (`true` = flush everything and speak now), `source` + `coalesce`/`kind` (see [coalescing](#coalescing-only-my-latest-matters)). Returns `{ok, id, position, state}` — plus `flushed` (how many queued items an interrupt deleted) and `coalesced` (ids your own coalesce dropped); `429` when the queue (depth 32) is full. |
| `POST /skip` | Cancel the current utterance; the next queued one plays. Returns `{ok, skipped}` — the id that was skipped, or `null`. Optional body `{"id": N}` skips **only if** item `N` is the one playing, so a late "skip mine" can't kill somebody else's utterance. (SSIP calls this `STOP`.) |
| `POST /stop` | Panic button: stop playback and drain the queue. Returns `{ok, cleared}`. (SSIP calls this `CANCEL` — note the inverted verbs if you have speech-dispatcher reflexes.) |
| `POST /pause` / `POST /resume` | Pause/resume. Queue-level: while paused, the next queued item won't start either. |
| `GET /queue` | Queue introspection: `{current, pending, depth, recent}`. Each entry carries its `source` (`null` when unset). `recent` holds the last 16 terminal outcomes — `done`, `canceled` (dropped before starting), `interrupted` (cut off mid-play), or `error` — so callers can learn the fate of a submitted `id`. |
| `GET /status` | Service state, pause flag, `queue_depth`, playback progress. |
| `GET /voices` | Available Azure voices (cached 5 min). |
| `GET /chronicle` | Read the [Chronicle](#the-chronicle). Query params: `limit` (default 20, capped at 500), `q` (case-insensitive substring), `kind` (`you` / `spoken`). Returns `{entries, enabled}`, oldest-first — ready to display. |
| `POST /respeak` | Play a Chronicle line again. Body `{"id": N}`; omit `id` to replay the last thing spoken. Returns `{ok, respeaking}`, or `404` on a bad id, an empty chronicle, or a full queue. |
| `GET /api/version` | [realm-sigil](https://github.com/jphein/realm-sigil) version contract (git-derived, with a minimal fallback when realm-sigil isn't installed). |

```bash
curl -X POST localhost:7710/speak -H 'Content-Type: application/json' \
  -d '{"text": "Hello from the queue", "voice": "en-US-JennyNeural"}'
```

### Coalescing: "only my latest matters"

With several agents narrating at once, a deep FIFO guarantees you hear **stale**
speech — forty queued *still working…* before the *done* you actually wanted.
Tag your requests with a `source` and set `coalesce: true`, and each new request
drops that source's own **unspoken** backlog:

```bash
curl -X POST localhost:7710/speak -H 'Content-Type: application/json' \
  -d '{"text": "Build 80 percent", "source": "ci", "coalesce": true}'
# -> {"ok":true,"id":7,"position":0,"state":"queued","coalesced":[5,6]}
```

Coalescing is deliberately narrow, so it is safe to use without coordinating
with anyone:

- It only ever drops items sharing **your** `source` — never another caller's.
- It never cuts off the utterance that is **already playing**; use `/skip` or
  `interrupt` for that.
- Dropped items are reported in `coalesced`, land in `GET /queue`'s `recent`
  ring as `canceled`, and log one INFO line with the ids.
- `coalesce`/`kind` without a `source` is a `400` — silently coalescing nothing
  would be worse than failing loudly.

`kind: "progress"` is accepted as an alias. Speech Dispatcher's SSIP protocol has
a dedicated `progress` class that drops intermediate updates but promotes the
*last* message of a burst so the final "100%" is always heard. In a strict FIFO
that promotion is free — dropping older same-source items always leaves the
newest, and the newest always gets spoken — so `kind: "progress"` is the same
machinery under a name that reads better at the call site.

## The Chronicle

Speech is the one interface with no scrollback — you hear it once and it's gone. The
Chronicle is an append-only ledger of everything said in both directions, so a line you
half-caught is still there afterwards.

Every final transcript is recorded as `kind: "you"`, and every utterance that actually
played as `kind: "spoken"` (carrying its `voice` and `source`). One JSON object per line at
`$XDG_STATE_HOME/gnome-speaks/chronicle.jsonl` (`~/.local/state/…` by default) — local
only, never synced, never in git:

```json
{"kind": "you", "text": "how much battery is left", "ts": "2026-08-17T21:04:11-0700", "id": 1787025851000}
{"kind": "spoken", "text": "The batteries hold 82 percent.", "ts": "2026-08-17T21:04:13-0700", "voice": "en-US-Ava:DragonHDLatestNeural", "source": "assistant", "id": 1787025853000}
```

Ids are millisecond timestamps, bumped on same-millisecond collisions, so they stay unique
without a persistent counter. Writes are best-effort by design: a full disk logs a warning
and must never take down the voice pipeline. A streamed AI reply is recorded as **one**
entry rather than per-sentence — sentence-level shards would be useless to respeak.

Five ways in:

| Seam | Use |
|---|---|
| 📜 rune on the badge | Unfurls a scroll of the last 8 lines, newest first — green for you, violet for it. Click a line: it flares gold and respeaks. Click outside to dismiss. |
| Panel menu → Chronicle | Submenu of the last 12 lines; click to hear one again. |
| `GET /chronicle` · `POST /respeak` | Scripted access (see [HTTP API](#http-api)). |
| `GetChronicle(limit)` · `Respeak(id)` | Same over D-Bus. `Respeak(0)` replays the last spoken line; `GetChronicle(0)` falls back to 20 entries. |
| "cast echo" · "cast chronicle" · "cast seal the chronicle" | Replay the last line · recite the last three · stop/resume recording. |

A respeak re-enters the normal speech queue rather than jumping it: a `spoken` line replays
in its **original** voice, while a `you` line is read back in the current default voice.

Recording is on by default and switchable in preferences ("Keep the Chronicle") or by
voice; while sealed, nothing is written at all.

## Spiel provider

gnome-speaks can register as a [Spiel](https://github.com/project-spiel) speech
provider (`org.gnome.Speaks.Speech.Provider`), making its voices available to
any libspiel client — notably Orca's experimental Spiel backend. Off by
default; in `~/.config/speech-to-cli/config.json`:

| Key | Default | Meaning |
|-----|---------|---------|
| `spiel_provider` | `false` | Own the provider bus name and serve `Synthesize`. |
| `spiel_voices` | `["en_GB-cori-high"]` | Curated Piper voices to advertise (the Wyoming host synthesizes them). |
| `spiel_expose_azure` | `false` | Also advertise the configured Azure voices — **metered**: a screen reader narrating your desktop through these costs real money per sentence. |

The provider writes raw S16LE PCM to the client's pipe (the client owns
playback, per Spiel's design), handles concurrent requests, and treats a
closed pipe as cancellation. No SSML or word-event support is advertised.

**Pointing Orca at it:** Orca ≥46 ships a Spiel backend, but note two traps.

First, libspiel is still not packaged on Ubuntu (26.04 included) — build from
source and make its typelib visible session-wide via
`~/.config/environment.d/`.

Second, *how* you select the backend depends on your Orca version:

- **Orca 50+** keeps settings in GSettings, and the old
  `orca.settings.speechSystemOverride` hook **no longer exists** — an
  `orca-customizations.py` that sets it is silently doing nothing. Use the
  relocatable schema instead (an old `speechServerFactory` in your settings is
  migrated to this key automatically):

  ```bash
  gsettings set "org.gnome.Orca.Speech:/org/gnome/orca/default/speech/" \
      speech-server-factory spiel
  ```

  Valid values are `speechdispatcherfactory` (the default) and `spiel`. Note the
  schema is relocatable, so the `:path` suffix is mandatory — plain
  `gsettings set org.gnome.Orca.Speech …` errors out. Swap `default` for your
  profile name if you use Orca profiles, and restart Orca.

- **Orca 46–49** initializes speech *before* applying `speechServerFactory` from
  user settings, so the reliable switch there is
  `orca.settings.speechSystemOverride = "spiel"` in
  `~/.local/share/orca/orca-customizations.py`.

## Voice Spellbook

Utterances beginning with a trigger word (`cast` or `invoke`) are **incantations**:
they route to a local spellbook instead of being typed or sent to the LLM. Works in
every mode; a realm chime confirms the match instantly (~2 ms) and spoken replies
ride the speech queue (during continuous dictation, replies wait for the mic to
close — casting never speaks over an open microphone). An unknown incantation
speaks "The spell fizzles" instead of silently typing.

Spells are data: the repo ships `spellbook.json` with the self-control spells;
a user overlay at `~/.config/speech-to-cli/spellbook.json` merges over it by
spell name and hot-reloads on save. POST spells can carry dictated
text: `"remainder_field": "body"` injects the words spoken after the pattern
into that body field, and `"reply"` speaks a fixed success line.

**Ships in the repo** (`spellbook.json`) — all self-control, no external endpoints:

| Incantation | Effect |
|---|---|
| "cast silence" / "cast skip" | stop everything / skip current utterance |
| "cast terminal mode" / "ai mode" / "type mode" | switch modes |
| "cast read notifications" | toggle the notification herald |
| "cast wake word" | arm or disarm the [waking watch](#wake-word) |
| "cast deep thought" | toggle extended LLM thinking |
| "cast subtitles" | toggle the caption overlay (writes both config and GSettings) |
| "cast echo" | replay the last thing spoken |
| "cast chronicle" | recite the last three [Chronicle](#the-chronicle) lines |
| "cast seal the chronicle" | stop or resume recording the Chronicle |

**Overlay examples** — these live in your own `~/.config/speech-to-cli/spellbook.json`
because their endpoints are site-specific:

| Incantation | Effect |
|---|---|
| "cast defense report" | combat-ward summary, spoken |
| "cast my level" | character sheet from realm progression |
| "cast recent events" | last 5 realm events, spoken |
| "cast mana reserves" | solar/battery/grid report |
| "cast consult the oracle \<question\>" | ask the realm Oracle; streamed reply |
| "cast torches \<request\>" | natural-language pass-through to Home Assistant Assist |

### Wake word

With a [Wyoming openwakeword](https://github.com/rhasspy/wyoming-openwakeword)
server on your LAN, the service can open the mic hands-free. Configure in
`~/.config/speech-to-cli/config.json`: `wake_word: true`, `wake_word_model`
(your openwakeword model name — effectively your wake phrase, so keep it out
of public places), `wyoming_wake_port` (default 10400), plus the same
`wyoming_host` used by the offline fallback. Armed **only while idle** — never
during dictation or TTS playback — and a detection behaves exactly like the
dictation keybinding (chime, current mode applies, "cast …" spells work).
Toggle by voice with "cast wake word". If the wake server is unreachable the
watcher backs off quietly and everything else keeps working.

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
