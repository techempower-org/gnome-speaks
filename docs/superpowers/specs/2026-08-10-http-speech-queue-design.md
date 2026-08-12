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

## Post-research amendments (2026-08-10, same day)

Three prior-art research agents (speech-dispatcher/SSIP, Project Spiel + Ubuntu,
cross-platform API survey — reports in
`~/.claude/projects/-home-jp/scratch/speech-queue-prior-art/`) drove four changes:

1. **`/pause` is queue-level** — the dispatcher's hold predicate includes the pause
   event. Unanimous prior art (Web Speech, .NET, Apple): a paused service must not
   start the next item. Without this, pausing at an item boundary let the next
   agent utterance play into a "paused" room.
2. **`interrupt` reports blast radius** — response gains `flushed: <n>`, and drained
   item ids log at INFO. (Android structurally forbids flushing other apps' speech;
   we allow it but make it leave a trace.)
3. **Per-item outcomes** — `GET /queue` gains `recent`: ring buffer (16) of
   `{id, outcome}` with `done` / `canceled` (never started) / `interrupted`
   (cut off mid-play) / `error`. One terminal outcome per item, vocabulary borrowed
   from Web Speech + Android. 5/5 surveyed APIs have completion reporting.
4. **SSIP verb mapping documented** — our `/skip`≈SSIP `STOP`, `/stop`≈SSIP `CANCEL`
   (inverted verbs); README notes it rather than renaming.

Deliberately deferred (filed as follow-up): per-source coalescing keys and a
`progress`-style drop-intermediate-keep-final class (SSIP's best ideas — real design
work, separate cycle), id-scoped `/skip` (1/5 precedent).
**Both landed 2026-08-12 — see "Coalescing + id-scoped skip" below.**

**Overflow policy — explicit decision (post-research):** the queue keeps
reject-newest (429) rather than drop-oldest. The SSIP thesis ("fresh speech beats
stale speech") argues for drop-oldest, but 429 gives the producer a synchronous,
actionable failure, while drop-oldest silently victimizes a different caller.
With the `recent` outcome ring, drop-oldest would at least be observable
(`canceled`), so if a stale-backlog problem materializes in practice, revisit
alongside per-source coalescing — coalescing is the better fix for the same
root cause.

**Known non-goal:** stop-at-word-boundary (Apple-only precedent) — `speech_tts.tts()`
exposes no mid-playback hook; not implementable without restructuring the audio path.

Also: "Ubuntu looking into it" = **Myna**, Canonical's speech-to-TEXT project
(17 Jun 2026, Ubuntu 26.10) — relevant to Type mode, not this queue. Spiel is a
per-app synthesis API with no cross-app arbitration — aligning with it would
recreate the stomping problem; not a fit.

## Coalescing + id-scoped skip (2026-08-12, issues #4 / #5)

The two deferred items above, implemented. Decisions worth recording:

1. **Coalescing is opt-in and source-scoped.** `POST /speak` takes `source`
   (string, truncated to 64 chars) and `coalesce: true`; enqueueing drops that
   source's own **unspoken** items and returns their ids as `coalesced`. It can
   never touch another caller's speech, which is what makes it safe to use
   without coordination — unlike `interrupt`, it needs no blast-radius warning.
2. **The playing item is never coalesced.** Only queued-but-unstarted items are
   dropped. Killing audio mid-word is `/skip`'s job, and conflating the two
   would make "post my latest status" occasionally chop a sentence in half.
3. **`kind: "progress"` is an alias, not a second mechanism.** SSIP's `progress`
   class drops intermediates but promotes the *last* of a burst so the final
   "100%" is always heard. Under our strict FIFO that promotion is free:
   drop-older-same-source always leaves the newest, and the newest is always
   spoken. Documented as equivalent rather than built twice (YAGNI).
4. **`coalesce`/`kind` without `source` is a 400.** Consistent with the 429
   reasoning: give the producer a synchronous, actionable failure instead of
   silently doing nothing.
5. **Overflow policy unchanged.** Reject-newest (429) stays. Coalescing is now
   the recommended fix for backlog pressure — it removes staleness at the root,
   so the overflow victim question mostly stops arising.
6. **`/skip` id scoping closes the race properly.** The id check and
   `cancel_active()` happen together under `_queue_current_lock`; the dispatcher
   takes that same lock to claim and clear the current item, so a verified item
   cannot be swapped out from under the cancel. Bodyless `/skip` keeps the old
   positional behaviour — when a human says "move on", positional is correct.

Concurrency notes: coalescing edits the `queue.Queue`'s underlying deque under
its `mutex` (the access pattern `GET /queue` already used). Safe because every
producer uses `put_nowait` (no blocked putter to wake) and `qsize()`/`maxsize`
read `len(queue)` live, so freed slots are real — verified by a test that fills
to 32, confirms `Full`, coalesces, and successfully puts again. A new
`_enqueue_lock` serializes coalesce-then-put so two concurrent same-source
bursts can't interleave into two survivors. Lock order is always
`_enqueue_lock → _tts_queue.mutex`.

Validation: `py_compile`, plus 60 assertions in
`~/.claude/projects/-home-jp/scratch/queue-coalescing/test_queue_logic.py`,
which AST-extracts the real `enqueue_speech` / `_coalesce_source` /
`skip_current` / `_handle_speak` / `_handle_skip` / `_handle_queue` bodies from
the service and execs them against stubs — the service itself can't be imported
(module-level `gi`/D-Bus/Azure side effects, hyphenated filename). Live curl
battery run at merge time.

## Docs Ripple (at ship time)

- README HTTP API section.
- Outside this repo: global `~/.claude/CLAUDE.md` line "interrupts any in-progress
  speech — it is not a queue" and the agent-orchestration skill's voice contract both
  become stale when this lands.
