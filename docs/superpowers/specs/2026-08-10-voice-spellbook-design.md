# Voice Spellbook — local voice commands as incantations

## Problem

Transcribed speech can only be typed or sent to an LLM. There is no fast local
command layer: no way to switch modes, query the realm, control the house, or
consult the resident narrators by voice. JP wants this layer to be novel and
magical — litrpg/fantasy-flavored — leveraging the existing ecosystem rather
than inventing a plain command list.

(Naming note: the existing `voice_commands` config flag is spoken-*punctuation*
substitution ("period" → "."). This feature is separate: the **spellbook**.)

## Research provenance (2026-08-10)

Three agents probed the ecosystem live (reports in
`~/.claude/projects/-home-jp/scratch/voice-spellbook/`):
- **realm-actions**: full realmwatch action inventory with measured latencies
  (direct HTTP 0.6–15 ms, `realm` CLI 370–510 ms, D-Bus `PlaySound` 2 ms);
  existing spell vocabulary in `os.realm.watch/skills/cast.md`; bugs filed as
  realmwatch#124 (/status broken), #125 (dual player records), #126 (unbounded
  /events).
- **voice-lore**: the Oracle (a LAN host) is live and in-persona; the Ashen
  Ledger (:8093) has a 50-voice catalog + director notes; D-Bus `Talk(text)` is
  the only two-way seam (speaks AND returns the spoken reply).
- **ha-desktop**: HA Assist rides the LAN Wyoming stack (a dedicated
  pipeline) and honors `conversation_id` (multi-turn); solar/battery are NOT
  exposed to Assist — use `realm ha energy --json`; GNOME privileged D-Bus is
  closed (gnome-speaks#7: `_get_focused_app()` silently dead); destructive
  inventory catalogued.

## Decisions (settled with JP 2026-08-10)

1. **Invocation = "cast" prefix.** Utterances beginning with a trigger word
   (`cast`, `invoke` — configurable) route to the spellbook in every mode
   (Type/Terminal/Loop/AI) and never reach typing or the LLM. No bare control
   words: barge-in already interrupts TTS on any speech. Zero dictation
   false-positives by construction.
2. **Architecture = in-service spellbook module** (`spellbook.py` beside the
   service; matching + execution in-process). Rejected: external daemon
   consuming `TranscriptionReady` (extra process, and the signal fires after
   mode routing — too late to intercept); LLM/Assist routing for everything
   (violates zero-latency/zero-token premise; Assist can't see solar/battery).
3. **Targets: all three** — gnome-speaks self-control, Home Assistant via
   Assist, desktop/realm actions.

## Recognition & routing

- Hook: in both STT result paths, on the **raw transcript**, before
  `apply_voice_commands` (punctuation substitution would mangle patterns) and
  before mode routing.
- Match: lowercase, strip trailing punctuation; utterance must start with a
  trigger word; remainder matched against each spell's `patterns` (exact
  phrases + aliases; homophones are just more aliases: "defence report").
  Parameterized spells use one trailing capture slot ("wake <host>"); hostnames
  resolve via realmwatch `fleet_resolve` (bare names 400 on `realm ping`).
- No fuzzy scoring in v1 — aliases are predictable and cheap.
- Miss (trigger word but no spell) → **fizzle**: speak a short in-genre failure
  ("the spell fizzles"); never type the text.
- **`POST /cast`** on :7710 accepts `{"text": "cast defense report"}` and runs
  the identical path — every spell testable by curl, castable by agents,
  subject to the same gates.

## Spellbook format & executor

Repo ships `spellbook.json`; user overlay at
`~/.config/speech-to-cli/spellbook.json` merges over it (per-spell by `name`).
Hot-reload on mtime, same pattern as `auto_corrections`.

```json
{
  "name": "defense-report",
  "patterns": ["defense report", "defence report", "how are the wards"],
  "action": {"type": "http", "method": "GET",
             "url": "http://<realm-host>/combat-ward/defense-report",
             "speak": "$.report"},
  "chime": "threat_alert",
  "speak_as": "en-US-Davis:DragonHDLatestNeural",
  "gate": "instant",
  "timeout_s": 3
}
```

Action types (v1, six): `http` (GET/POST; `speak` is a dotted field path into
the JSON response), `dbus_self` (service's own methods: stop/skip/mode flags),
`shell` (**allowlisted argv arrays only** — for `realm` CLI verbs; no shell
strings, no interpolation), `assist` (remainder → HA `conversation.process`;
`response_type` action_done/query_answer = success, error = fizzle), `oracle`
(POST + SSE → sentences to queue), `say` (canned themed response).

Feedback discipline:
- Chime fires **at match time** (PlaySound, 2 ms) — before the action runs.
- Spoken results go through the service's **own HTTP queue**
  (`localhost:7710/speak`, `voice=speak_as`) — spells inherit PR #3's
  ordering/preemption semantics; a chatty spell can never stomp dictation.

## Safety

- `gate: "instant"` — read-only/reversible. The entire v1 list.
- `gate: "confirm"` — executor speaks a challenge via D-Bus `Talk()` and
  proceeds only on a reply containing "confirm". (Combat-ward: voice may
  `propose` only — never `approve`/`execute`.)
- **Hardcoded executor denylist** (not a config tier — entries touching these
  refuse to load, with a log line): `homeassistant/restart`,
  `switch.goodwe_off_grid`, `script.solar_ems_apply`, Battery-Emulator reboot,
  `automation.turn_off` on safety reflexes, `script.all_lights_off`,
  `valve.*`/`lock.*`/`alarm_control_panel.*`, realmwatch `POST /ssh`,
  combat-ward approve/execute, `wol sleep`, ydotool typing, screen lock.
- HA auth: token via env `HA_TOKEN` → `~/.cache/ha-token-tmp` → `bw get
  password ha-llat` (existing chain). Never logged.

## Error handling

Per-action timeout (default 3 s) → fizzle + `threat_alert` chime suppressed to
avoid alarm fatigue; unreachable target → "the realm is beyond reach"; every
cast logs one structured line: `spell | matched pattern | action type | outcome
| latency_ms`. Spellbook load errors (bad JSON, denylisted action) log loudly
and skip the entry — a broken spell never breaks the service.

## v1 spells (10, all instant)

| Spell | Patterns (canonical) | Action |
|---|---|---|
| silence | "silence" | dbus_self Stop |
| skip | "skip" | dbus_self (HTTP /skip semantics) |
| terminal mode / ai mode / type mode | "terminal mode" … | dbus_self mode flags |
| read notifications | "read notifications" | dbus_self |
| defense report | "defense report" | http → speak $.report |
| mana reserves | "mana reserves", "power report" | shell `realm ha energy --json` → themed speech |
| my level | "my level", "character sheet" | http `/progression/player` (realmwatch#125 pins this endpoint) |
| recent events | "recent events" | http `/events?limit=5` |
| consult the oracle <q> | "consult the oracle …" | oracle POST+SSE |
| torches <...> | "torches …", "lights …" | assist passthrough of remainder |

## Deferred (explicitly)

- **Ember story commands (v1.1)**: continue-story (bump `buffer_target`),
  played-chapter-N (`listened`), fill-the-buffer — three distinct commands per
  the engine's throttle semantics; do not conflate.
- `org.gnome.Speaks.Desktop` interface exported from extension.js (window
  mgmt/screenshots; also fixes gnome-speaks#7 properly).
- Wake-word casting ("oh realm…" with no hotkey) — sub-project 4.
- Per-source coalescing for chatty spells — gnome-speaks#4.
- New chime WAVs (only 4 exist; adding files needs zero code).

## Validation

`py_compile`; restart; then via `POST /cast`: each v1 spell as a curl
one-liner; fizzle path; denylist-refusal log line; Assist round-trip
(`response_type` mapping); Oracle SSE→speech; journal shows structured cast
lines. Live mic pass at the end (JP speaks 2–3 spells).
