# Voice Spellbook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Spoken incantations ("cast defense report") trigger local actions with chime-on-match and themed spoken replies through the speech queue.

**Architecture:** New `spellbook.py` module (loader + matcher + executor with injected callbacks — no GLib/audio deps of its own) + data-driven `spellbook.json` (repo default, user overlay, mtime hot-reload). The service hooks `_try_cast()` into both STT result paths before `apply_voice_commands`/mode routing, wires callbacks (chime→`play_sound`, speak→`enqueue_speech`, confirm→`talk`), and exposes `POST /cast` for mic-less testing.

**Tech Stack:** Python 3 stdlib only (`json`, `re`, `urllib.request`, `subprocess`). No test framework (project convention): validation = `py_compile` + inline `python3 -c` sanity runs + `POST /cast` curl battery + journal.

**Spec:** `docs/superpowers/specs/2026-08-10-voice-spellbook-design.md`. Research: `~/.claude/projects/-home-jp/scratch/voice-spellbook/*.md` (exact endpoint shapes for oracle/HA live there — consult before finalizing network spells).

**File map:**
- Create `spellbook.py` — loader, DENYLIST validation, matcher, SpellExecutor (6 action types)
- Create `spellbook.json` — the 10 v1 spells
- Modify `gnome-speaks-service.py` — import, init wiring, `skip_current()`, `_spell_ctx_dbus()`, `_spell_speak()`, `_try_cast()`, two STT-path hooks, `_handle_cast` + route, `_get_ha_token()`
- Modify `README.md`, `install.sh` (must copy spellbook.py/json if it copies the service file — check)

---

### Task 1: spellbook.py core — loader, denylist, matcher (+ spellbook.json skeleton)

**Files:** Create `spellbook.py`, `spellbook.json`

- [ ] **Step 1: Write `spellbook.py`** with module docstring, imports (`json,l ogging, os, re, subprocess, time, urllib.request, urllib.error`), `log = logging.getLogger("gnome-speaks")`, constants:

```python
DENYLIST = [
    r"homeassistant/restart", r"goodwe_off_grid", r"solar_ems_apply",
    r"battery[_-]?emulator", r"automation\.turn_off", r"all_lights_off",
    r"\bvalve\.", r"\block\.", r"alarm_control_panel\.", r"/ssh\b",
    r"combat-ward/(approve|execute)", r"wol\s+sleep", r"ydotool",
    r"loginctl\s+lock",
]
VALID_ACTION_TYPES = ("say", "dbus_self", "http", "shell", "assist", "oracle")
VALID_GATES = ("instant", "confirm")
FIZZLE_TEXT = "The spell fizzles."
UNREACHABLE_TEXT = "The realm is beyond reach."

class SpellUnreachable(Exception):
    """Target service down/timeout — spoken as UNREACHABLE_TEXT."""
```

`load_spellbook(repo_path, user_path)` → `{"trigger_words": [...], "spells": {name: spell}}`: read each file (repo then user), `json.load` guarded (log.error + continue on failure), user overlay merges per-spell by `name`, `trigger_words` lowercased if present. Each spell passes `_validate(spell)` → error string or None: requires `name`, `patterns`, `action.type` in VALID_ACTION_TYPES, `gate` in VALID_GATES, shell actions need non-empty `argv` list, and `_denied(action)` (regex search of DENYLIST against `json.dumps(action)`, case-insensitive) must be None. Rejected spells log loudly and are skipped. Final `log.info` with count + triggers.

`match(text, book)` → `(kind, spell, remainder)` with kind ∈ `"miss" | "fizzle" | "cast"`: normalize (strip, lower, strip trailing `[.,!?;:]+`), split words; if first word not in trigger_words → miss. Join rest as incantation; empty → fizzle. For each spell × pattern (lowered): exact match → remainder `""`; prefix match (`pattern + " "`) → remainder is the tail. Longest pattern wins. No match → `("fizzle", None, incantation)`.

- [ ] **Step 2: Sanity-run the matcher**

```bash
python3 -c "
import spellbook, json
book = {'trigger_words': ['cast'], 'spells': {'dr': {'name':'dr','patterns':['defense report'],'action':{'type':'say'}}, 'o': {'name':'o','patterns':['consult the oracle'],'action':{'type':'say'}}}}
assert spellbook.match('Hello world', book)[0] == 'miss'
assert spellbook.match('Cast defense report.', book)[0] == 'cast'
assert spellbook.match('cast consult the oracle what is love', book)[2] == 'what is love'
assert spellbook.match('cast frobnicate', book)[0] == 'fizzle'
assert spellbook.match('cast', book)[0] == 'fizzle'
print('MATCH-OK')
"
```
Expected: `MATCH-OK`

- [ ] **Step 3: Sanity-check denylist validation**

```bash
python3 -c "
import spellbook
bad = {'name':'evil','patterns':['x'],'action':{'type':'http','url':'http://x/ssh'}}
assert spellbook._validate(bad) is not None
ok = {'name':'good','patterns':['x'],'action':{'type':'say','text':'hi'}}
assert spellbook._validate(ok) is None
print('DENY-OK')
"
```
Expected: `DENY-OK`

- [ ] **Step 4: Write `spellbook.json`** with trigger_words `["cast","invoke"]` and the six *local* spells (network spells land in Task 4 once endpoint shapes are confirmed from the research files):

```json
{
  "trigger_words": ["cast", "invoke"],
  "spells": [
    {"name": "silence", "patterns": ["silence", "be silent", "quiet"],
     "action": {"type": "dbus_self", "op": "stop"}, "chime": null, "gate": "instant"},
    {"name": "skip", "patterns": ["skip", "next"],
     "action": {"type": "dbus_self", "op": "skip"}, "gate": "instant"},
    {"name": "terminal-mode", "patterns": ["terminal mode"],
     "action": {"type": "dbus_self", "op": "terminal_mode", "reply": "Terminal mode."}, "chime": "xp_gain", "gate": "instant"},
    {"name": "ai-mode", "patterns": ["ai mode", "conversation mode"],
     "action": {"type": "dbus_self", "op": "ai_mode", "reply": "A I mode."}, "chime": "xp_gain", "gate": "instant"},
    {"name": "type-mode", "patterns": ["type mode", "dictation mode"],
     "action": {"type": "dbus_self", "op": "type_mode", "reply": "Type mode."}, "chime": "xp_gain", "gate": "instant"},
    {"name": "read-notifications", "patterns": ["read notifications", "notifications"],
     "action": {"type": "dbus_self", "op": "read_notifications_toggle", "reply": "Notification herald toggled."}, "chime": "xp_gain", "gate": "instant"}
  ]
}
```

- [ ] **Step 5: Commit**

```bash
python3 -c "import py_compile; py_compile.compile('spellbook.py', doraise=True)" && python3 -c "import json; json.load(open('spellbook.json'))"
git add spellbook.py spellbook.json && git commit -m "feat: spellbook core — loader, denylist, incantation matcher"
```

---

### Task 2: SpellExecutor — six action types

**Files:** Modify `spellbook.py`

- [ ] **Step 1: Add `SpellExecutor`** — constructor takes injected callbacks `chime(name)`, `speak(text, voice=None)`, `dbus_self(op)`, `confirm(prompt)->str`, `ha_token()->str|None`. Public `cast(spell, remainder)`:
  - `t0 = time.monotonic()`; fire `chime` immediately if set (match-time feedback).
  - `gate == "confirm"`: `reply = confirm(f"Confirm casting {name}?")`; proceed only if `"confirm" in reply.lower()`, else speak "The casting is stayed." and log outcome `declined`.
  - Dispatch `getattr(self, "_do_" + action["type"])(action, remainder, spell)` inside try/except: `SpellUnreachable` → speak `UNREACHABLE_TEXT`, outcome `unreachable`; other exceptions → log.exception, speak `FIZZLE_TEXT`, outcome `error`.
  - Structured log always: `log.info("CAST | %s | %s | %s | %dms", name, action_type, outcome, ms)`.

  Handlers (all take `(action, remainder, spell)`, speak via `spell.get("speak_as")`):
  - `_do_say`: speak `action["text"]`.
  - `_do_dbus_self`: call `self._dbus_self(action["op"])`; speak optional `action.get("reply")`.
  - `_do_http`: GET/POST `action["url"]` with `urllib.request` (POST body = `action.get("body", {})` as JSON), timeout `spell.get("timeout_s", 3)`; URLError/timeout → `SpellUnreachable`. Parse JSON; `action.get("speak")` is a dotted path (`$.report` → strip `$.`, walk dict keys); speak result if truthy.
  - `_do_shell`: `subprocess.run(action["argv"], capture_output=True, text=True, timeout=spell.get("timeout_s", 5))` — argv list only, never a string. Timeout → `SpellUnreachable`; nonzero rc → RuntimeError(stderr[:200]). If `action.get("speak_template")`: try `json.loads(stdout)` then `template.format(**payload)`, fall back to raw stdout[:300]; else speak stdout[:300] if nonempty.
  - `_do_assist`: requires `ha_token()` (else SpellUnreachable) and nonempty remainder (else speak "Speak the ritual's object.", outcome `fizzle`). POST `{"text": remainder, "language": "en"}` to `action["url"]` with `Authorization: Bearer <token>`, timeout 5. Map `response.response_type`: `error` → speak HA's speech or FIZZLE_TEXT, outcome `fizzle`; else speak HA's `response.speech.plain.speech` or "It is done.", outcome `done`.
  - `_do_oracle`: requires nonempty remainder (else speak "The Oracle awaits your question.", `fizzle`). POST `{field: remainder}` (field from `action.get("field","message")`) to `action["url"]`; iterate SSE lines (`data:` prefix), accumulate text (`json.loads(chunk).get("text","")` with plain-string fallback), split on `[.!?]\s` and speak each sentence as it completes, flush tail. Timeout `spell.get("timeout_s", 30)`.

- [ ] **Step 2: Sanity-run executor with fake callbacks**

```bash
python3 -c "
import spellbook
spoken = []
ex = spellbook.SpellExecutor(chime=lambda n: spoken.append(('chime', n)),
    speak=lambda t, v=None: spoken.append(('speak', t)),
    dbus_self=lambda op: spoken.append(('op', op)),
    confirm=lambda p: 'confirm', ha_token=lambda: None)
ex.cast({'name':'greet','patterns':['x'],'action':{'type':'say','text':'hail'},'chime':'xp_gain'}, '')
assert ('chime','xp_gain') in spoken and ('speak','hail') in spoken
out = ex.cast({'name':'torch','patterns':['x'],'action':{'type':'assist','url':'https://<ha-host>/api/conversation/process'}}, 'lights')
assert out == 'unreachable'  # no token in this env
print('EXEC-OK')
"
```
Expected: `EXEC-OK`

- [ ] **Step 3: Commit**

```bash
python3 -c "import py_compile; py_compile.compile('spellbook.py', doraise=True)"
git add spellbook.py && git commit -m "feat: spellbook executor — say/dbus_self/http/shell/assist/oracle actions"
```

---

### Task 3: Service integration — hooks, /cast, skip_current, token chain

**Files:** Modify `gnome-speaks-service.py`

- [ ] **Step 1:** `import spellbook` (after the stdlib imports; same directory). Module-level `_get_ha_token()`: env `HA_TOKEN` → file `~/.cache/ha-token-tmp` → `subprocess.run(["bw","get","password","ha-llat"], timeout=10)`; return None on all failures; never log the value.

- [ ] **Step 2: Service methods** (add after `play_sound`):

```python
    def skip_current(self):
        """Cancel the current queued utterance only; next plays (voice /skip)."""
        with self._queue_current_lock:
            current = self._queue_current
        if current is not None:
            state.cancel_active()
            return current.id
        return None

    def _spell_speak(self, text, voice=None):
        """Spell replies ride the speech queue — same ordering/preemption as agents."""
        if text:
            try:
                self.enqueue_speech(text, voice=voice)
            except queue.Full:
                log.warning("Spell reply dropped: speech queue full")

    def _spell_ctx_dbus(self, op):
        if op == "stop":
            self.stop()
        elif op == "skip":
            self.skip_current()
        elif op == "terminal_mode":
            self._save_config_flag("terminal_mode", True)
            self._save_config_flag("conversation_mode", False)
        elif op == "ai_mode":
            self._save_config_flag("conversation_mode", True)
            self._save_config_flag("terminal_mode", False)
        elif op == "type_mode":
            self._save_config_flag("conversation_mode", False)
            self._save_config_flag("terminal_mode", False)
        elif op == "read_notifications_toggle":
            self._save_config_flag("read_notifications",
                                   not CONFIG.get("read_notifications", False))
        else:
            raise ValueError(f"unknown dbus_self op: {op}")
```

Refactor `_handle_skip` to use `skip_current()` (DRY):

```python
    def _handle_skip(self):
        """Cancel the current queued utterance only; the next one plays."""
        skipped = self.service.skip_current()
        self._send_json({"ok": True, "skipped": skipped})
```

- [ ] **Step 3: Init wiring** (end of `__init__`, after the queue block):

```python
        # Voice spellbook (incantation layer) — "cast …" routes here
        self._spellbook_paths = (
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "spellbook.json"),
            os.path.expanduser("~/.config/speech-to-cli/spellbook.json"),
        )
        self._spellbook = spellbook.load_spellbook(*self._spellbook_paths)
        self._spellbook_mtimes = self._spellbook_stat()
        self._spell_executor = spellbook.SpellExecutor(
            chime=self.play_sound, speak=self._spell_speak,
            dbus_self=self._spell_ctx_dbus, confirm=self.talk,
            ha_token=_get_ha_token)
```

Plus helpers:

```python
    def _spellbook_stat(self):
        return tuple(os.path.getmtime(p) if os.path.isfile(p) else 0
                     for p in self._spellbook_paths)

    def _maybe_reload_spellbook(self):
        mtimes = self._spellbook_stat()
        if mtimes != self._spellbook_mtimes:
            self._spellbook = spellbook.load_spellbook(*self._spellbook_paths)
            self._spellbook_mtimes = mtimes

    def _try_cast(self, text):
        """Route 'cast …' utterances to the spellbook. True = consumed."""
        self._maybe_reload_spellbook()
        kind, spell, remainder = spellbook.match(text, self._spellbook)
        if kind == "miss":
            return False
        if kind == "fizzle":
            log.info("CAST | fizzle | nothing matched %r", (remainder or "")[:60])
            self._spell_speak(spellbook.FIZZLE_TEXT)
            return True
        threading.Thread(target=self._spell_executor.cast,
                         args=(spell, remainder), daemon=True,
                         name=f"spell-{spell['name']}").start()
        return True
```

- [ ] **Step 4: Hook both STT paths.** Streaming path (search `apply_voice_commands(user_text)` first occurrence, ~line 983): immediately after `user_text` is extracted and before `apply_voice_commands`, insert:

```python
            if user_text and self._try_cast(user_text):
                GLib.idle_add(self._emit_transcription_ready, user_text)
                self._set_state("idle")
                _schedule_warmup()
                return
```

Batch/loop path (second occurrence, ~line 1360, inside step "8. Post-process"): insert the cast check *before* voice-command post-processing, preserving loop semantics — if `is_loop`, re-enter listening instead of returning idle:

```python
            if user_text and self._try_cast(user_text):
                GLib.idle_add(self._emit_transcription_ready, user_text)
                if is_loop and not self._stop_event.is_set():
                    self._set_state("listening")
                    continue
                self._set_state("idle")
                _schedule_warmup()
                return
```

(Adapt `continue`/`return` to the enclosing loop/function structure found at the site — the batch path is a loop; verify with Read before editing.)

- [ ] **Step 5: `POST /cast`** — handler + route in `do_POST` before the 404 fallback:

```python
    def _handle_cast(self):
        body = self._read_json_body()
        if body is None:
            return
        text = body.get("text", "")
        if not text or not text.strip():
            self._send_error_json(400, "Missing or empty 'text' field")
            return
        handled = self.service._try_cast(text)
        self._send_json({"ok": True, "handled": handled})
```

```python
        elif path == "/cast":
            self._handle_cast()
```

- [ ] **Step 6: install.sh check** — `grep -n "gnome-speaks-service.py" install.sh`; if it copies the service file anywhere, add `spellbook.py` + `spellbook.json` alongside. (systemd runs from the repo, so this may be a no-op.)

- [ ] **Step 7: Validate + commit**

```bash
python3 -c "import py_compile; py_compile.compile('gnome-speaks-service.py', doraise=True)"
systemctl --user restart gnome-speaks.service && sleep 2 && systemctl --user is-active gnome-speaks.service
# journal must show "Spellbook loaded: 6 spells"
journalctl --user -u gnome-speaks.service -n 20 --no-pager | grep -i spellbook
# miss passes through:
curl -s -X POST localhost:7710/cast -H 'Content-Type: application/json' -d '{"text":"hello there"}'   # {"handled": false}
# fizzle:
curl -s -X POST localhost:7710/cast -H 'Content-Type: application/json' -d '{"text":"cast frobnicate"}'  # handled true + speaks fizzle
# mode spell + config flip:
curl -s -X POST localhost:7710/cast -H 'Content-Type: application/json' -d '{"text":"cast terminal mode"}'
python3 -c "import json; print(json.load(open('/home/jp/.config/speech-to-cli/config.json'))['terminal_mode'])"  # True
curl -s -X POST localhost:7710/cast -H 'Content-Type: application/json' -d '{"text":"cast type mode"}'
# skip spell against a queued utterance; journal shows CAST lines
git add gnome-speaks-service.py install.sh && git commit -m "feat: wire spellbook into STT paths, add POST /cast seam"
```

---

### Task 4: Network spells — realm scrying, oracle, torches

**Files:** Modify `spellbook.json`

- [ ] **Step 1: Confirm endpoint shapes from research files** (they contain exact probed calls): `grep -A5 -iE "defense-report|progression/player|events\?limit|oracle" ~/.claude/projects/-home-jp/scratch/voice-spellbook/realm-actions.md ~/.claude/projects/-home-jp/scratch/voice-spellbook/voice-lore.md ~/.claude/projects/-home-jp/scratch/voice-spellbook/ha-desktop.md` — take URL, method, response field names from there, not from memory. realmwatch base is `map_server.py :80`; the realm host per homelab table.

- [ ] **Step 2: Add the four scrying + two consultation spells** (URLs/fields per Step 1; shapes below are the template):

```json
    {"name": "defense-report", "patterns": ["defense report", "defence report", "how are the wards"],
     "action": {"type": "http", "method": "GET", "url": "<from research>", "speak": "<field path>"},
     "chime": "threat_alert", "gate": "instant", "timeout_s": 3},
    {"name": "my-level", "patterns": ["my level", "character sheet", "my character"],
     "action": {"type": "http", "method": "GET", "url": "<progression/player>", "speak": "<computed>"},
     "chime": "xp_gain", "gate": "instant"},
    {"name": "recent-events", "patterns": ["recent events", "what has happened"],
     "action": {"type": "http", "method": "GET", "url": "<events?limit=5>", "speak": "<field>"},
     "gate": "instant"},
    {"name": "mana-reserves", "patterns": ["mana reserves", "power report", "energy report"],
     "action": {"type": "shell", "argv": ["<realm-cli-abs-path>", "ha", "energy", "--json"],
                "speak_template": "<from actual JSON keys>"},
     "gate": "instant", "timeout_s": 8},
    {"name": "consult-oracle", "patterns": ["consult the oracle", "ask the oracle", "oracle"],
     "action": {"type": "oracle", "url": "<oracle endpoint>", "field": "<from research>"},
     "chime": "level_up", "gate": "instant", "timeout_s": 30},
    {"name": "torches", "patterns": ["torches", "lights", "upon the house"],
     "action": {"type": "assist", "url": "https://<ha-host>/api/conversation/process"},
     "gate": "instant", "timeout_s": 6}
```

Notes: `my-level` may need a compact speakable field — if `/progression/player` has no single sentence field, use a `speak_template`-style composition via `http` `speak` walking to name/level, or fall back to `shell` + `realm` CLI with template. `recent-events`: if the response is a list, `_do_http` speaks nothing useful — extend `_extract` to join a list field's top-level `summary`/`message` strings (≤5) with "; ". Implement that in `_do_http`'s `_extract` helper when hit.

- [ ] **Step 3: Test each read-only spell via /cast** (each returns handled:true, speaks, and logs `CAST | <name> | … | done`):

```bash
for s in "cast defense report" "cast my level" "cast recent events" "cast mana reserves"; do
  curl -s -X POST localhost:7710/cast -H 'Content-Type: application/json' -d "{\"text\":\"$s\"}"; echo; sleep 6; done
curl -s -X POST localhost:7710/cast -H 'Content-Type: application/json' -d '{"text":"cast consult the oracle what wards protect this realm"}'
# torches: use a QUERY, not an action, to avoid flipping real lights during tests:
curl -s -X POST localhost:7710/cast -H 'Content-Type: application/json' -d '{"text":"cast torches are the office lights on"}'
journalctl --user -u gnome-speaks.service -n 30 --no-pager | grep "CAST |"
```

- [ ] **Step 4: Commit**

```bash
python3 -c "import json; json.load(open('spellbook.json'))"
git add spellbook.json spellbook.py && git commit -m "feat: v1 network spells — realm scrying, oracle consultation, HA torches"
```

---

### Task 5: README + ship

- [ ] **Step 1: README** — add "## Voice Spellbook" section after the HTTP API section: trigger words, table of the 10 v1 spells, user overlay path, `POST /cast` seam, safety model (instant/confirm/denylist), fizzle semantics. Match existing README style.
- [ ] **Step 2:** Re-run the full /cast battery on the final tree; `git status` clean; commit README (`docs: document the voice spellbook`).
- [ ] **Step 3:** Push branch, PR (`feat: voice spellbook — incantation layer over STT`), body summarizes spells + safety + research provenance; merge per full-auto; checkout main; pull; restart service; verify journal "Spellbook loaded".
- [ ] **Step 4:** External docs: agent-orchestration skill gains one line — agents may cast via `POST /cast` subject to the same gates. Live mic pass: JP speaks 2–3 spells (report what to try).

---

## Self-review notes

- Spec coverage: prefix matching (T1), fizzle (T1/T3), /cast (T3), six actions (T2), denylist (T1), chime-at-match (T2), speak-via-queue (T3 `_spell_speak`), hot-reload (T3), confirm gate via Talk (T2/T3 wiring), HA token chain (T3), 10 spells (T1+T4), README (T5). Gap: none found; Ember commands explicitly deferred per spec.
- Network spell URLs deliberately resolved from the research files at execution time (Task 4 Step 1) rather than hardcoded here — the reports contain the probed truth; the plan's JSON shapes are templates for those values. This is a controlled exception to "no placeholders": the values exist on disk, the step says exactly where.
- Type consistency: `match()` returns `(kind, spell, remainder)` everywhere; executor `cast(spell, remainder)`; callbacks named identically in T2 definition and T3 wiring.
