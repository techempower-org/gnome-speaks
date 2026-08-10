# HTTP Speech Queue

## Problem

`POST /speak` interrupts any in-progress speech — it is not a queue. Multiple agents
speaking through `localhost:7710` stomp each other mid-sentence. Additionally, the
per-request overrides are racy: `_handle_speak` swaps `svc._voice_quality` on the shared
service object and restores it in `finally` immediately after `speak()` returns, but the
worker thread reads `self._voice_quality` later — overlapping requests can read the wrong
value. `_pending_output_file` has the same one-shot-global race.

## Decisions (settled with JP 2026-08-10)

1. **Queue by default.** `POST /speak` appends; playback is FIFO. `"interrupt": true`
   flushes current + queue and speaks immediately. Breaking change to the documented
   interrupt semantics — docs updated at ship time.
2. **User preempts, queue holds.** User-initiated speech (D-Bus `Speak`, clipboard,
   selection, AI-mode replies) interrupts the current queued utterance immediately. The
   interrupted item is dropped; the rest of the queue pauses and resumes after user
   speech finishes.
3. **`/stop` flushes everything; new `/skip` cancels current item only.**

## Architecture (Approach A — dispatcher thread)

All changes in `gnome-speaks-service.py`. No new files, no D-Bus interface changes, no
changes to the AI streaming sentence pipeline or `speak()`'s public signature.

- **`TTSQueueItem`** dataclass: `id` (monotonic counter), `text`, `voice`, `quality`,
  `output_file`, `enqueued_at`. Per-utterance overrides travel with the item, retiring
  the `_voice_quality` swap and `_pending_output_file` global (race fix).
- **`self._tts_queue = queue.Queue(maxsize=32)`** — HTTP `/speak` enqueues; full → 429.
- **Dispatcher thread** (daemon, started at service init):
  `queue.get()` → wait until clear (no `_user_speech_active`, state not
  `listening`/`processing` — its own `speaking` state during back-to-back playback
  does not block) → play synchronously via `_speak_item(item)` → next. `_speak_item` is the current
  `_speak_worker` body refactored to read the item instead of service globals.
- **Preemption wiring:** the user path (`speak()`) sets a `_user_speech_active` event on
  entry, clears it on completion. Its existing `stop()` call kills in-flight playback via
  the cancel event; the dispatcher drops the dead item and blocks on the event.
- **Listening interplay:** dispatcher holds while state is `listening`/`processing` —
  queued speech never plays over an open mic. Half-duplex drain applies unchanged
  (playback still goes through `speech_tts.tts()`).
- **State machine:** states unchanged. When more items are pending, the dispatcher skips
  the `speaking → idle → speaking` flap between items (no badge flicker).

## API Contract (localhost:7710)

| Endpoint | Change | Response |
|----------|--------|----------|
| `POST /speak` | Queues by default; optional `"interrupt": true` = flush all + speak now | `{ok, id, position, state}` — `position: 0` = playing now, `state` ∈ `"speaking"`,`"queued"`; `429 {ok:false, error:"queue full"}` |
| `POST /skip` | New — cancel current utterance, next plays | `{ok, skipped: <id\|null>}` |
| `POST /stop` | Also drains queue (panic button) | `{ok, cleared: <n>}` |
| `GET /queue` | New — introspection | `{current, pending: [{id, text≤80ch, voice, enqueued_at}], depth}` |
| `GET /status` | Gains `queue_depth` | existing shape + one field |

`/pause` and `/resume` unchanged — they act on current playback; the dispatcher is
naturally blocked while an item plays.

## Error Handling

- TTS failure on an item: log, emit D-Bus `Error` signal, continue to the next item.
  One bad utterance never wedges the queue.
- Queue full: HTTP 429. Empty text: 400 (existing).
- Queue is in-memory; lost on service restart. Correct for ephemeral voice chatter.

## Validation (no test framework — live checks per project convention)

1. `python3 -c "import py_compile; py_compile.compile('gnome-speaks-service.py', doraise=True)"`
2. `systemctl --user restart gnome-speaks.service`, then:
   - Two rapid `POST /speak` → both play, serially.
   - `/speak` ×3 + `/skip` → only current dropped; rest play.
   - `/speak` ×3 + `/stop` → silence; `GET /queue` shows depth 0.
   - `"interrupt": true` → flushes and speaks immediately.
   - D-Bus `Speak` mid-queue → preempts; queue resumes after.
   - Start dictation with non-empty queue → queue holds until idle.

## Docs Ripple (at ship time)

- README HTTP API section.
- Outside this repo: global `~/.claude/CLAUDE.md` line "interrupts any in-progress
  speech — it is not a queue" and the agent-orchestration skill's voice contract both
  become stale when this lands.
