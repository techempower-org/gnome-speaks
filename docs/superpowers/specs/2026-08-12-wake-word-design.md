# Wake word — hands-free activation via LAN openwakeword

## Problem

Every voice interaction starts with a keyboard shortcut. A wake word makes the
whole stack hands-free: say the word, the mic opens, dictate or cast spells.
A Wyoming openwakeword server already runs on the LAN with JP's custom-trained
model loaded (name lives in the user config) — verified live via a `describe` handshake.

## Decisions (JP, 2026-08-12)

1. **Model: JP's custom openwakeword model** (already loaded on the server; its name — effectively the wake phrase — stays in the user config, not in this public repo).
2. **Idle-only arming (v1).** Detection is honored only when the service is
   idle — no dictation running, no TTS playing. Wake-word barge-in during
   speech is explicitly deferred (echo story first).
3. **Detection = the dictation hotkey.** `start_listening(quick=True)`, chime
   included — identical to pressing the keybinding. Whatever mode is active
   (Type/Terminal/AI) applies, and "cast …" spells work.
4. **Split as in the Wyoming fallback:** protocol in speech-to-cli
   (`wyoming.detect_stream`), lifecycle in gnome-speaks (watcher thread).
   Config keys in the user config only; repo defaults leave the feature off
   and the model name empty (both repos are public).

## Components

### `wyoming.detect_stream(host, port, model, chunk_iter, timeout=5.0)` (speech-to-cli)

Streams raw s16le 16 kHz mono chunks from `chunk_iter` as `audio-chunk`
events after a `detect` (names=[model]) + `audio-start` handshake. A reader
thread blocks on server events (avoids the select-vs-buffered-reader trap);
a `detection` event sets a flag and the function returns the detection name.
`chunk_iter` ending (caller disarms) returns None. Socket errors raise
`WyomingError`. Config whitelist gains `wake_word` (false), `wake_word_model`
(""), `wyoming_wake_port` (10400).

### Wake watcher thread (gnome-speaks service)

Daemon thread started at init. Each cycle:
- Sleep-poll (0.5 s) until `CONFIG["wake_word"]` is true, `wyoming_host` and
  `wake_word_model` are set, and state == idle.
- Spawn its own recorder (`_build_rec_cmd()` — pw-record 16 kHz s16 mono to
  stdout; PipeWire supports concurrent capture with the prewarmed STT
  recorder).
- Feed a generator into `detect_stream` that stops yielding the moment the
  flag turns off or state leaves idle (socket closes, cycle restarts).
- On detection while still idle: log, `GLib.idle_add(start_listening,
  quick=True)`, 2 s cooldown.
- `WyomingError` (wake server down): 60 s backoff, log at most every 5 min.
- Recorder always killed with SIGKILL (pw-record ignores SIGTERM — known
  gotcha).

`_SYNC_FLAGS` gains `wake_word` and `wake_word_model` so config edits (prefs,
spell) propagate live into the running service.

### Toggle spell

New `dbus_self` op `wake_word_toggle`; repo `spellbook.json` gains:
patterns "wake word" / "waking watch" → toggles the flag, replies "The waking
watch is toggled.", `xp_gain` chime.

## Error handling

Watcher failures never crash the service (broad catch → 10 s pause). The wake server going
down degrades to no-wake-word with periodic retry; everything else unaffected.
Detection while state changed mid-flight (race between event and idle check)
is dropped — the 0.5 s poll re-arms.

## Validation

1. Headless: synthesize the wake phrase with Piper (and Azure
   voices if Piper doesn't trigger), resample to 16 kHz, feed through
   `detect_stream` → expect the detection name. Negative phrase → None.
   (A custom model may only trigger reliably on JP's voice — if synthetic
   audio can't trigger it, the headless check is skipped as inconclusive,
   not failed.)
2. Live: enable `wake_word` in config, speak the wake phrase near the mic → chime +
   badge listening; dictation lands. Speak it during TTS playback → nothing
   (idle-only). "cast wake word" toggles the watcher off and on.
3. Journal shows arming/detection/backoff lines; service stays `active`
   with the wake server unreachable (simulated by a bogus
   `wyoming_wake_port`).

## Deferred

Wake-word barge-in during TTS; multiple wake words with per-word actions
(one model → dictation, a second → AI mode); on-katana openwakeword
fallback when the LAN wake server is down.
