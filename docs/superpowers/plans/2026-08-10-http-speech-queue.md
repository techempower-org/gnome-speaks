# HTTP Speech Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `POST /speak` queue FIFO instead of interrupting, with user preemption, `/skip`, `/stop`-drains-all, and `GET /queue` introspection.

**Architecture:** One `queue.Queue(maxsize=32)` + one daemon dispatcher thread in `gnome-speaks-service.py` that plays HTTP items serially through a parameterized `_speak_worker`. User speech paths (D-Bus `Speak`/`Talk`, clipboard, selection, AI streaming) set a `_user_speech_active` event that holds the dispatcher; they preempt via `stop(drain_queue=False)`. Per-item payload (voice/quality/output_file) replaces the racy service-global overrides.

**Tech Stack:** Python 3 stdlib only (`queue`, `threading`, `dataclasses`, `itertools`). No test framework (project convention) — validation is `py_compile` + service restart + curl/dbus-send live checks. Spec: `docs/superpowers/specs/2026-08-10-http-speech-queue-design.md`.

**Context facts** (verified 2026-08-10):
- systemd runs the repo file directly: `ExecStart=/home/jp/Projects/gnome-speaks/gnome-speaks-service.py`. Service changes need only `systemctl --user restart gnome-speaks.service`. No extension.js changes in this plan → no shell restart needed.
- `import queue` exists (line ~32). `itertools` and `dataclasses` do NOT — Task 1 adds them.
- A post-commit hook auto-deploys to the extensions dir (harmless here; docs+service only).
- Line numbers below are from commit `c62bcb0`; they shift as tasks land — anchor by symbol name.

---

### Task 1: Queue foundation — dataclass, service fields, drain helper, parameterized worker, preemption wiring

Behavior-neutral: the queue exists but nothing enqueues yet. HTTP still calls `speak()` (changed in Task 3).

**Files:**
- Modify: `gnome-speaks-service.py` — imports (~line 22-36), new `TTSQueueItem` before `class GnomeSpeaksService` (~line 600), `__init__` (~line 611), `speak()` (~line 1452), `_speak_worker()` (~line 1536), `talk()` (~line 1647), `stop()` (~line 2251)

- [ ] **Step 1: Add imports**

After `import sys` in the import block, add:

```python
import itertools
from dataclasses import dataclass
```

- [ ] **Step 2: Add `TTSQueueItem` dataclass immediately above `class GnomeSpeaksService`**

```python
@dataclass
class TTSQueueItem:
    """One queued HTTP speech request. Per-item overrides travel with the
    item so overlapping HTTP requests can't race on service globals."""
    id: int
    text: str
    voice: str | None = None        # Azure ShortName override
    quality: str | None = None      # "fast" | "hd" | None = service default
    output_file: str | None = None  # save-to-disk instead of playback
    enqueued_at: float = 0.0
```

- [ ] **Step 3: Add queue fields to `GnomeSpeaksService.__init__`**

After the `self._http_progress_lock = threading.Lock()` line, add:

```python
        # HTTP speech queue — agents on :7710 queue FIFO instead of stomping
        # each other. User speech paths set _user_speech_active to hold the
        # dispatcher (user outranks agents).
        self._tts_queue = queue.Queue(maxsize=32)
        self._tts_queue_seq = itertools.count(1)  # next() is atomic in CPython
        self._queue_current = None                # TTSQueueItem now playing
        self._queue_current_lock = threading.Lock()
        self._user_speech_active = threading.Event()
        threading.Thread(target=self._tts_dispatcher, daemon=True,
                         name="tts-queue-dispatcher").start()
```

- [ ] **Step 4: Add `enqueue_speech`, `_drain_tts_queue`, `_tts_dispatcher` methods**

Add these three methods to `GnomeSpeaksService`, directly above `def speak(`:

```python
    def enqueue_speech(self, text, voice=None, quality=None, output_file=None):
        """Create and enqueue an HTTP speech item for serial playback.

        Returns (item_id, position) where position is the number of items
        ahead (0 = will play next). Raises queue.Full at capacity.
        """
        item = TTSQueueItem(
            id=next(self._tts_queue_seq),
            text=text.strip(),
            voice=voice,
            quality=quality,
            output_file=output_file,
            enqueued_at=time.time(),
        )
        with self._queue_current_lock:
            busy = self._queue_current is not None
        position = self._tts_queue.qsize() + (1 if busy else 0)
        self._tts_queue.put_nowait(item)
        return item.id, position

    def _drain_tts_queue(self):
        """Remove all pending speech-queue items. Returns the count cleared."""
        cleared = 0
        while True:
            try:
                self._tts_queue.get_nowait()
                cleared += 1
            except queue.Empty:
                if cleared:
                    log.info("Drained %d queued speech item(s)", cleared)
                return cleared

    def _tts_dispatcher(self):
        """Daemon thread: plays queued HTTP speech items serially.

        Holds while user speech is active or the mic/LLM is busy. Its own
        'speaking' state between back-to-back items does NOT hold it (the
        idle flap is suppressed via suppress_idle to avoid badge flicker).
        """
        while True:
            item = self._tts_queue.get()
            while (self._user_speech_active.is_set()
                   or self.current_state in ("listening", "processing")):
                time.sleep(0.2)
            with self._queue_current_lock:
                self._queue_current = item
            try:
                if self.current_state != "speaking":
                    self._set_state("speaking")
                has_next = not self._tts_queue.empty()
                # Runs synchronously in this thread — playback is the wait.
                self._speak_worker(item.text, voice=item.voice,
                                   quality=item.quality,
                                   output_file=item.output_file,
                                   user_initiated=False,
                                   suppress_idle=has_next)
            except Exception:
                log.exception("Speech queue: item %d failed, continuing", item.id)
            finally:
                with self._queue_current_lock:
                    self._queue_current = None
```

- [ ] **Step 5: Parameterize `_speak_worker`**

Change the signature from `def _speak_worker(self, text, voice=None):` to:

```python
    def _speak_worker(self, text, voice=None, quality=None, output_file=None,
                      user_initiated=True, suppress_idle=False):
```

Inside, apply these exact changes:

a) Replace both quality reads with a local. After `state._cancel_event.clear()` add `q = quality or self._voice_quality`, then change:
- `speed_factor = 22.0 if self._voice_quality == "fast" else 15.0` → `speed_factor = 22.0 if q == "fast" else 15.0`
- `result = speech_tts.tts(text, quality=self._voice_quality, ...` → `result = speech_tts.tts(text, quality=q, ...`

b) Replace the output_file global with the parameter:
- `output_file=getattr(self, '_pending_output_file', None))` → `output_file=output_file)`
- Delete the line `self._pending_output_file = None` and its comment `# Clear one-shot output file after use`.

c) In the `finally:` block, guard the idle transition and hands-free restart, and release the user hold. Change:

```python
            if still_speaking:
                self._set_state("idle")
            _schedule_warmup()
```

to:

```python
            if still_speaking and not suppress_idle:
                self._set_state("idle")
            _schedule_warmup()
            if user_initiated:
                self._user_speech_active.clear()
```

and change the hands-free condition `if (CONFIG.get("continuous_dictation", False)` to `if (user_initiated and CONFIG.get("continuous_dictation", False)` — queued agent chatter must not re-open the mic.

- [ ] **Step 6: Wire preemption into `speak()`**

In `speak()`, replace:

```python
        # Stop outside lock to prevent deadlock (stop() acquires multiple locks)
        if self.current_state not in ("idle",):
            self.stop()

        if not CONFIG.get("key"):
            GLib.idle_add(self._emit_error, "Azure Speech key not configured")
            return False
```

with:

```python
        # Hold the queue dispatcher before preempting (user outranks agents).
        # The worker's finally clears this; early exits must clear it here.
        self._user_speech_active.set()

        # Stop outside lock to prevent deadlock (stop() acquires multiple locks)
        # drain_queue=False: user preemption drops only the current utterance,
        # the agent backlog survives and resumes after user speech.
        if self.current_state not in ("idle",):
            self.stop(drain_queue=False)

        if not CONFIG.get("key"):
            GLib.idle_add(self._emit_error, "Azure Speech key not configured")
            self._user_speech_active.clear()
            return False
```

- [ ] **Step 7: Wire preemption into `talk()`**

`talk()` blocks until done, so a try/finally in the method covers all exits. Replace:

```python
        if not text or not text.strip():
            return "error: no text provided"

        with self._talk_lock:
            if self.current_state not in ("idle",):
                self.stop()
```

with:

```python
        if not text or not text.strip():
            return "error: no text provided"

        self._user_speech_active.set()
        try:
            with self._talk_lock:
                if self.current_state not in ("idle",):
                    self.stop(drain_queue=False)
```

Indent the rest of the `with self._talk_lock:` body one level, and after the block (aligned with `try:`) add:

```python
        finally:
            self._user_speech_active.clear()
```

- [ ] **Step 8: Add `drain_queue` parameter to `stop()`**

Change `def stop(self):` and the docstring to:

```python
    def stop(self, drain_queue=True):
        """Stop any current operation and return to idle.

        drain_queue: also flush pending HTTP speech-queue items (panic stop —
        D-Bus Stop and POST /stop). User preemption passes False so the agent
        backlog survives and resumes afterwards.
        """
        log.info("Stop requested (current state: %s)", self.current_state)
        if drain_queue:
            self._drain_tts_queue()
```

(then the existing body continues with `self._stop_event.set()`).

- [ ] **Step 9: Syntax check + restart + regression check**

Run: `python3 -c "import py_compile; py_compile.compile('gnome-speaks-service.py', doraise=True)" && systemctl --user restart gnome-speaks.service && sleep 2 && systemctl --user is-active gnome-speaks.service`
Expected: `active`

Run: `dbus-send --session --dest=org.gnome.Speaks --print-reply /org/gnome/Speaks org.gnome.Speaks.GetState`
Expected: reply with `string "idle"`

Run: `curl -s -X POST localhost:7710/speak -H 'Content-Type: application/json' -d '{"text":"Task one regression check."}'`
Expected: `{"ok": true, "state": "speaking"}` (old response — HTTP not switched yet), speech audible, `journalctl --user -u gnome-speaks.service -n 20` shows no tracebacks.

- [ ] **Step 10: Commit**

```bash
git add gnome-speaks-service.py
git commit -m "feat: add TTS queue foundation — dispatcher, per-item payload, user preemption"
```

---

### Task 2: Hold the dispatcher during AI streaming replies

AI conversation replies stream sentence-by-sentence through their own TTS path; without this, queued agent items can play over an in-progress AI reply (dispatcher only holds on listening/processing, and streaming happens in "speaking").

**Files:**
- Modify: `gnome-speaks-service.py` — `_stream_conversation_worker()` (~line 2047)

- [ ] **Step 1: Set the hold at worker start, clear in a new `finally`**

At the top of `_stream_conversation_worker(self, user_text)`, immediately after the docstring, add:

```python
        # AI replies are user-initiated speech: hold the agent speech queue
        # for the whole turn (LLM streaming + sentence TTS).
        self._user_speech_active.set()
```

The method body is one big `try: ... except Exception as exc: ...`. Append a `finally:` clause to that same try statement (after the except block, same indentation as `try:`):

```python
        finally:
            self._user_speech_active.clear()
```

- [ ] **Step 2: Syntax check + restart**

Run: `python3 -c "import py_compile; py_compile.compile('gnome-speaks-service.py', doraise=True)" && systemctl --user restart gnome-speaks.service && sleep 2 && systemctl --user is-active gnome-speaks.service`
Expected: `active`

- [ ] **Step 3: Commit**

```bash
git add gnome-speaks-service.py
git commit -m "feat: hold speech queue during AI streaming replies"
```

---

### Task 3: Switch HTTP endpoints to the queue

Activates the feature: `/speak` enqueues (+`interrupt` flag, 429 on full), `/skip` and `GET /queue` added, `/stop` reports cleared count, `/status` gains `queue_depth`. Removes the racy `_voice_quality` swap and `_pending_output_file` global.

**Files:**
- Modify: `gnome-speaks-service.py` — `do_GET` (~line 2349), `do_POST` (~line 2358), `_handle_speak` (~line 2387), `_handle_stop` (~line 2420), `_handle_status` (~line 2444), new `_handle_skip` + `_handle_queue`

- [ ] **Step 1: Replace `_handle_speak` entirely**

```python
    def _handle_speak(self):
        body = self._read_json_body()
        if body is None:
            return  # error already sent
        text = body.get("text", "")
        if not text or not text.strip():
            self._send_error_json(400, "Missing or empty 'text' field")
            return

        svc = self.service
        quality = body.get("quality") if body.get("quality") in ("fast", "hd") else None
        voice = body.get("voice") or None
        output_file = body.get("output_file") or None

        # interrupt: true — flush everything and speak now (panic + speak)
        if body.get("interrupt"):
            svc._drain_tts_queue()
            svc.stop(drain_queue=False)

        try:
            item_id, position = svc.enqueue_speech(
                text, voice=voice, quality=quality, output_file=output_file)
        except queue.Full:
            self._send_error_json(429, "queue full")
            return

        state_str = ("speaking" if position == 0
                     and svc.current_state == "idle" else "queued")
        self._send_json({"ok": True, "id": item_id,
                         "position": position, "state": state_str})
```

- [ ] **Step 2: Replace `_handle_stop`, add `_handle_skip` and `_handle_queue`**

Replace `_handle_stop` with:

```python
    def _handle_stop(self):
        svc = self.service
        cleared = svc._drain_tts_queue()
        svc.stop(drain_queue=False)
        self._send_json({"ok": True, "state": "idle", "cleared": cleared})

    def _handle_skip(self):
        """Cancel the current queued utterance only; the next one plays."""
        svc = self.service
        with svc._queue_current_lock:
            current = svc._queue_current
        if current is None:
            self._send_json({"ok": True, "skipped": None})
            return
        state.cancel_active()
        self._send_json({"ok": True, "skipped": current.id})

    def _handle_queue(self):
        svc = self.service
        with svc._queue_current_lock:
            cur = svc._queue_current
        current = None
        if cur is not None:
            current = {"id": cur.id, "text": cur.text[:80], "voice": cur.voice,
                       "enqueued_at": cur.enqueued_at}
        with svc._tts_queue.mutex:
            items = list(svc._tts_queue.queue)
        pending = [{"id": i.id, "text": i.text[:80], "voice": i.voice,
                    "enqueued_at": i.enqueued_at} for i in items]
        self._send_json({"current": current, "pending": pending,
                         "depth": len(pending)})
```

- [ ] **Step 3: Route the new endpoints**

In `do_GET`, add before the 404 fallback:

```python
        elif path == "/queue":
            self._handle_queue()
```

In `do_POST`, add before the 404 fallback:

```python
        elif path == "/skip":
            self._handle_skip()
```

- [ ] **Step 4: Add `queue_depth` to `_handle_status`**

In `_handle_status`, change:

```python
        result = {"state": current, "paused": paused}
```

to:

```python
        result = {"state": current, "paused": paused,
                  "queue_depth": svc._tts_queue.qsize()}
```

- [ ] **Step 5: Syntax check + restart**

Run: `python3 -c "import py_compile; py_compile.compile('gnome-speaks-service.py', doraise=True)" && systemctl --user restart gnome-speaks.service && sleep 2 && systemctl --user is-active gnome-speaks.service`
Expected: `active`

- [ ] **Step 6: Live validation battery**

```bash
# Serial playback: enqueue two, second must report queued/position 1
curl -s -X POST localhost:7710/speak -H 'Content-Type: application/json' -d '{"text":"Queue test one. This sentence takes a few seconds to say."}'
curl -s -X POST localhost:7710/speak -H 'Content-Type: application/json' -d '{"text":"Queue test two."}'
curl -s localhost:7710/queue
```
Expected: first → `{"ok":true,"id":1,"position":0,"state":"speaking"}`; second → `"position":1,"state":"queued"`; `/queue` shows `current` set and one pending. Then journalctl shows both played sequentially.

```bash
# Skip: 3 items, skip current, verify next plays and one remains
for i in one two three; do curl -s -X POST localhost:7710/speak -H 'Content-Type: application/json' -d "{\"text\":\"Skip test $i. Padding words to lengthen playback.\"}"; done
curl -s -X POST localhost:7710/skip
curl -s localhost:7710/queue
```
Expected: `/skip` → `{"ok":true,"skipped":<id>}`; `/queue` shows the later items still pending/playing.

```bash
# Stop drains: enqueue 3, stop, verify cleared count and empty queue
for i in a b c; do curl -s -X POST localhost:7710/speak -H 'Content-Type: application/json' -d "{\"text\":\"Stop test $i with some padding words here.\"}"; done
curl -s -X POST localhost:7710/stop
curl -s localhost:7710/queue; curl -s localhost:7710/status
```
Expected: `/stop` → `cleared` ≥ 1; `/queue` → `{"current":null,"pending":[],"depth":0}`; status `queue_depth: 0`.

```bash
# Interrupt flag: enqueue 2, interrupt-speak, verify queue flushed
curl -s -X POST localhost:7710/speak -H 'Content-Type: application/json' -d '{"text":"Interrupt victim one, long enough to still be playing."}'
curl -s -X POST localhost:7710/speak -H 'Content-Type: application/json' -d '{"text":"Interrupt victim two."}'
curl -s -X POST localhost:7710/speak -H 'Content-Type: application/json' -d '{"text":"I interrupted everything.","interrupt":true}'
curl -s localhost:7710/queue
```
Expected: last call → `position 0`; `/queue` → only the interrupting item.

```bash
# User preemption: agent speech playing, D-Bus Speak preempts, queue resumes
curl -s -X POST localhost:7710/speak -H 'Content-Type: application/json' -d '{"text":"Agent one speaking a fairly long sentence for preemption testing."}'
curl -s -X POST localhost:7710/speak -H 'Content-Type: application/json' -d '{"text":"Agent two waits its turn."}'
dbus-send --session --dest=org.gnome.Speaks --print-reply /org/gnome/Speaks org.gnome.Speaks.Speak string:"User speech preempts now"
sleep 1; curl -s localhost:7710/queue
```
Expected: agent-one playback dies immediately, user speech plays, `/queue` still holds agent two, which plays after. Verify order in `journalctl --user -u gnome-speaks.service -n 40`.

```bash
# Voice override still works per-item (regression for PR #2)
curl -s -X POST localhost:7710/speak -H 'Content-Type: application/json' -d '{"text":"Voice override check.","voice":"en-US-JennyNeural"}'
```
Expected: plays in Jenny voice; no errors in journal.

- [ ] **Step 7: Commit**

```bash
git add gnome-speaks-service.py
git commit -m "feat(http): queue speech by default — /skip, /queue, stop drains, 429 on full"
```

---

### Task 4: README documentation

**Files:**
- Modify: `README.md` — the HTTP REST API section (locate with `grep -n "7710" README.md`)

- [ ] **Step 1: Update the HTTP API docs**

Rewrite the endpoint list in the existing style to document: queue-by-default `/speak` (body: `text`, optional `voice`, `quality`, `output_file`, `interrupt`), response `{ok, id, position, state}`, 429 on full (depth 32); `POST /skip`; `POST /stop` (drains, returns `cleared`); `GET /queue`; `GET /status` `queue_depth` field; user speech preempts and holds the queue. Match the README's existing formatting conventions — check before writing.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document HTTP speech queue API"
```

---

### Task 5: Ship + external docs ripple

- [ ] **Step 1: Final verification** — re-run the full Task 3 Step 6 battery on the final tree; `git log --oneline` shows the 4 commits; working tree clean.
- [ ] **Step 2: Push branch, open PR** (repo: techempower-org/gnome-speaks; branch `feat/http-speech-queue`; conventional PR title `feat(http): speech queue with user preemption`).
- [ ] **Step 3: External docs (post-merge):** update `~/.claude/CLAUDE.md` speech-to-cli MCP note ("interrupts any in-progress speech — it is not a queue" → queue semantics + `/skip`/`/queue`), and the agent-orchestration skill's voice contract.
- [ ] **Step 4: Return to `main`** (per CLAUDE.md branch hygiene).

---

## Self-review notes

- Spec coverage: queue-by-default+interrupt (T3S1), preemption+hold (T1S6-7, T2), /skip+/stop (T3S2), GET /queue + status depth (T3S2/S4), race fix via per-item payload (T1S5, T3S1), badge flap suppression (T1S4 `suppress_idle`), listening hold (T1S4 wait loop), error isolation per item (T1S4 try/except), 429 (T3S1), docs (T4, T5S3). No gaps found.
- Known accepted race: the dispatcher's hold check is a 0.2s poll — a mic open racing the check can collide for <0.2s; barge-in/half-duplex logic already tolerates this class of overlap. Not worth a mutex redesign.
- Semantic change beyond spec (deliberate, documented): hands-free auto-restart after TTS now fires only for user-initiated speech — queued agent chatter must not re-open the mic mid-conversation-loop.
