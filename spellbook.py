#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Voice spellbook — incantation matching and execution for gnome-speaks.

Utterances beginning with a trigger word ("cast …") route here instead of
typing or the LLM. Spells are data (spellbook.json in the repo, with a user
overlay at ~/.config/speech-to-cli/spellbook.json); this module matches and
executes them through callbacks injected by the service, so it has no GLib
or audio dependencies of its own and can be sanity-tested standalone.
"""

import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

log = logging.getLogger("gnome-speaks")

# Actions that must never be voice-castable, no matter what the config says.
# Matched (case-insensitive) against the JSON-serialized action; an entry
# matching any pattern refuses to load. A hardcoded floor, not a config tier.
DENYLIST = [
    r"homeassistant/restart",
    r"goodwe_off_grid",
    r"solar_ems_apply",
    r"battery[_-]?emulator",
    r"automation\.turn_off",
    r"all_lights_off",
    r"\bvalve\.",
    r"\block\.",
    r"alarm_control_panel\.",
    r"/ssh\b",
    r"combat-ward/(approve|execute)",
    r"wol\s+sleep",
    r"ydotool",
    r"loginctl\s+lock",
]

VALID_ACTION_TYPES = ("say", "dbus_self", "http", "shell", "assist", "oracle")
VALID_GATES = ("instant", "confirm")

FIZZLE_TEXT = "The spell fizzles."
UNREACHABLE_TEXT = "The realm is beyond reach."


class SpellUnreachable(Exception):
    """Target service down or timed out — spoken as UNREACHABLE_TEXT."""


def _denied(action):
    """Return the denylist pattern an action matches, or None."""
    blob = json.dumps(action)
    for pat in DENYLIST:
        if re.search(pat, blob, re.I):
            return pat
    return None


def _validate(spell):
    """Return an error string for a bad spell entry, or None if loadable."""
    if not spell.get("name"):
        return "missing name"
    if not spell.get("patterns"):
        return "missing patterns"
    action = spell.get("action") or {}
    if action.get("type") not in VALID_ACTION_TYPES:
        return f"unknown action type {action.get('type')!r}"
    if spell.get("gate", "instant") not in VALID_GATES:
        return f"unknown gate {spell.get('gate')!r}"
    pat = _denied(action)
    if pat:
        return f"action matches denylist ({pat})"
    if action["type"] == "shell" and (
            not isinstance(action.get("argv"), list) or not action["argv"]):
        return "shell action requires a non-empty argv list"
    return None


def load_spellbook(repo_path, user_path):
    """Load the repo spellbook and merge the user overlay by spell name.

    Returns {"trigger_words": [...], "spells": {name: spell}}. Invalid
    entries are skipped with a loud log line — a broken spell (or a broken
    file) never breaks the service.
    """
    book = {"trigger_words": ["cast", "invoke"], "spells": {}}
    for path, source in ((repo_path, "repo"), (user_path, "user")):
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            log.error("Spellbook %s (%s): unreadable: %s", path, source, exc)
            continue
        if data.get("trigger_words"):
            book["trigger_words"] = [w.lower() for w in data["trigger_words"]]
        for spell in data.get("spells", []):
            err = _validate(spell)
            if err:
                log.error("Spellbook (%s): spell %r rejected: %s",
                          source, spell.get("name"), err)
                continue
            book["spells"][spell["name"]] = spell
    log.info("Spellbook loaded: %d spells, triggers %s",
             len(book["spells"]), book["trigger_words"])
    return book


def match(text, book):
    """Match a raw transcript against the spellbook.

    Returns (kind, spell, remainder):
      kind "miss"   — no trigger word; the utterance is normal dictation.
      kind "fizzle" — trigger word spoken but no spell matched (spell=None,
                      remainder=the unmatched incantation).
      kind "cast"   — spell matched; remainder is the captured parameter
                      text ("" for exact matches). Longest pattern wins.
    """
    norm = text.strip().lower()
    norm = re.sub(r"[.,!?;:]+$", "", norm).strip()
    words = norm.split()
    if not words or words[0] not in book["trigger_words"]:
        return "miss", None, None
    incantation = " ".join(words[1:])
    if not incantation:
        return "fizzle", None, ""
    best = None  # (pattern_len, spell, remainder)
    for spell in book["spells"].values():
        for pattern in spell["patterns"]:
            p = pattern.lower().strip()
            if incantation == p:
                cand = (len(p), spell, "")
            elif incantation.startswith(p + " "):
                cand = (len(p), spell, incantation[len(p):].strip())
            else:
                continue
            if best is None or cand[0] > best[0]:
                best = cand
    if best:
        return "cast", best[1], best[2]
    return "fizzle", None, incantation


def _extract(payload, path):
    """Walk a dotted field path ("$.report", "report.text", or "$" for the
    whole payload) into a JSON payload. Lists of dicts are summarized: each
    item's text/summary/message (or first string value), up to 5 items.
    Returns a speakable string or ""."""
    if not path:
        return ""
    node = payload
    rest = path.lstrip("$").lstrip(".")
    if rest:
        for key in rest.split("."):
            if isinstance(node, dict):
                node = node.get(key)
            else:
                return ""
            if node is None:
                return ""
    if isinstance(node, list):
        parts = []
        for item in node[:5]:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = (item.get("text") or item.get("summary")
                        or item.get("message"))
                if not text:
                    for v in item.values():
                        if isinstance(v, str) and v:
                            text = v
                            break
                if text:
                    parts.append(text)
        return "; ".join(parts)
    if isinstance(node, dict):
        return ""
    return str(node)


class SpellExecutor:
    """Executes matched spells through service-injected callbacks.

    Callbacks (all required):
      chime(name)               -- fire-and-forget realm chime (~2 ms)
      speak(text, voice=None)   -- enqueue TTS on the speech queue
      dbus_self(op)             -- service self-ops: stop/skip/mode flags
      confirm(prompt) -> str    -- blocking Talk(): speak, return spoken reply
      ha_token() -> str|None    -- HA long-lived token (never logged)
    """

    def __init__(self, chime, speak, dbus_self, confirm, ha_token):
        self._chime = chime
        self._speak = speak
        self._dbus_self = dbus_self
        self._confirm = confirm
        self._ha_token = ha_token

    def cast(self, spell, remainder):
        """Execute one spell; returns the outcome string it logged."""
        t0 = time.monotonic()
        if spell.get("chime"):
            try:
                self._chime(spell["chime"])
            except Exception:
                log.debug("Chime %s failed", spell.get("chime"))
        voice = spell.get("speak_as")
        if spell.get("gate", "instant") == "confirm":
            reply = self._confirm(
                "Confirm casting %s?" % spell["name"].replace("-", " ")) or ""
            if "confirm" not in reply.lower():
                self._speak("The casting is stayed.", voice)
                return self._log(spell, "declined", t0)
        try:
            handler = getattr(self, "_do_" + spell["action"]["type"])
            outcome = handler(spell["action"], remainder, spell)
        except SpellUnreachable as exc:
            log.warning("Spell %s unreachable: %s", spell["name"], exc)
            self._speak(UNREACHABLE_TEXT, voice)
            outcome = "unreachable"
        except Exception as exc:
            log.exception("Spell %s failed: %s", spell["name"], exc)
            self._speak(FIZZLE_TEXT, voice)
            outcome = "error"
        return self._log(spell, outcome, t0)

    def _log(self, spell, outcome, t0):
        log.info("CAST | %s | %s | %s | %dms", spell["name"],
                 spell["action"]["type"], outcome,
                 int((time.monotonic() - t0) * 1000))
        return outcome

    # -- Action handlers ----------------------------------------------------

    def _do_say(self, action, remainder, spell):
        self._speak(action.get("text", ""), spell.get("speak_as"))
        return "done"

    def _do_dbus_self(self, action, remainder, spell):
        self._dbus_self(action["op"])
        if action.get("reply"):
            self._speak(action["reply"], spell.get("speak_as"))
        return "done"

    def _do_http(self, action, remainder, spell):
        url = action["url"]
        data = None
        if action.get("method", "GET").upper() == "POST":
            data = json.dumps(action.get("body", {})).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(
                    req, timeout=spell.get("timeout_s", 3)) as resp:
                payload = json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SpellUnreachable(str(exc))
        template = action.get("speak_template")
        if template:
            try:
                text = template.format(**payload)
            except (KeyError, IndexError, ValueError):
                text = ""
        else:
            text = _extract(payload, action.get("speak"))
        if text:
            self._speak(text, spell.get("speak_as"))
        return "done"

    def _do_shell(self, action, remainder, spell):
        # argv list only — never a shell string, never interpolated.
        try:
            proc = subprocess.run(action["argv"], capture_output=True,
                                  text=True, timeout=spell.get("timeout_s", 5))
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise SpellUnreachable(str(exc))
        if proc.returncode != 0:
            raise RuntimeError(
                proc.stderr.strip()[:200] or "exit %d" % proc.returncode)
        out = proc.stdout.strip()
        template = action.get("speak_template")
        text = ""
        if template and out:
            try:
                text = template.format(**json.loads(out))
            except (ValueError, KeyError, IndexError):
                text = out[:300]
        elif out:
            text = out[:300]
        if text:
            self._speak(text, spell.get("speak_as"))
        return "done"

    def _do_assist(self, action, remainder, spell):
        token = self._ha_token()
        if not token:
            raise SpellUnreachable("no Home Assistant token available")
        if not remainder:
            self._speak("Speak the ritual's object.", spell.get("speak_as"))
            return "fizzle"
        body = json.dumps({"text": remainder, "language": "en"}).encode()
        req = urllib.request.Request(
            action["url"], data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(
                    req, timeout=spell.get("timeout_s", 6)) as resp:
                payload = json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SpellUnreachable(str(exc))
        response = payload.get("response", {})
        speech = (response.get("speech", {}).get("plain", {})
                  .get("speech", ""))
        if response.get("response_type") == "error":
            self._speak(speech or FIZZLE_TEXT, spell.get("speak_as"))
            return "fizzle"
        self._speak(speech or "It is done.", spell.get("speak_as"))
        return "done"

    def _do_oracle(self, action, remainder, spell):
        if not remainder:
            self._speak("The Oracle awaits your question.",
                        spell.get("speak_as"))
            return "fizzle"
        body = json.dumps(
            {action.get("field", "message"): remainder}).encode()
        req = urllib.request.Request(
            action["url"], data=body,
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(
                    req, timeout=spell.get("timeout_s", 30)) as resp:
                buf = ""
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk in ("", "[DONE]"):
                        continue
                    try:
                        parsed = json.loads(chunk)
                        chunk = (parsed.get("text") or parsed.get("content")
                                 or parsed.get("delta") or "")
                    except ValueError:
                        pass  # plain-string SSE payload
                    buf += chunk
                    while True:
                        m = re.search(r"[.!?]\s", buf)
                        if not m:
                            break
                        sentence, buf = buf[:m.end()].strip(), buf[m.end():]
                        if sentence:
                            self._speak(sentence, spell.get("speak_as"))
                if buf.strip():
                    self._speak(buf.strip(), spell.get("speak_as"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SpellUnreachable(str(exc))
        return "done"
