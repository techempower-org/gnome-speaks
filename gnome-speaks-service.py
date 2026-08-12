#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# GNOME Speaks — TTS/STT floating badge for GNOME Shell
# Copyright (C) 2025 JP Hein
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""
GNOME Speaks — Session DBus service providing TTS and STT via Azure Speech Services.

Bus name:    org.gnome.Speaks
Object path: /org/gnome/Speaks
Interface:   org.gnome.Speaks

Uses speech-to-cli modules for all audio optimizations:
prewarmed recorder, persistent WebSocket, noise calibration caching,
HTTP session pooling, VAD, energy-gated silence detection.
"""

import argparse
import http.server
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import itertools
from collections import deque
from dataclasses import dataclass
import queue

import spellbook
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import uuid

import gi
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

# ---------------------------------------------------------------------------
# Import speech modules from speech-to-cli
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SPEECH_ENGINE = os.environ.get("SPEECH_ENGINE_PATH", os.path.expanduser("~/Projects/speech-to-cli"))
if not os.path.isdir(_SPEECH_ENGINE):
    print(f"Error: speech-to-cli not found at {_SPEECH_ENGINE}", file=sys.stderr)
    print("Set SPEECH_ENGINE_PATH or clone https://github.com/techempower-org/speech-to-cli", file=sys.stderr)
    sys.exit(1)
sys.path.insert(0, _SPEECH_ENGINE)

# ---------------------------------------------------------------------------
# Import cloud-chat-assistant path for LLM providers
# ---------------------------------------------------------------------------

_CCA_PATH = os.environ.get("CLOUD_CHAT_PATH", os.path.expanduser("~/Projects/cloud-chat-assistant"))

# Import unified LLM streaming library from cloud-chat-assistant
if os.path.isdir(_CCA_PATH):
    if _CCA_PATH not in sys.path:
        sys.path.insert(0, _CCA_PATH)
    try:
        from llm_stream import stream_chat, LLMStreamError  # noqa: E402
    except ImportError:
        stream_chat = None  # noqa: E402
        LLMStreamError = Exception  # noqa: E402
else:
    stream_chat = None
    LLMStreamError = Exception

import state  # noqa: E402
from state import CONFIG, HAS_VAD, HAS_WS, HAS_WHISPER, FRAME_BYTES, FRAME_MS, SAMPLE_RATE  # noqa: E402
import wyoming as wyoming_mod  # noqa: E402  (speech-to-cli engine module)
import spiel_provider  # noqa: E402  (imports state/wyoming — needs sys.path above)
from audio import (  # noqa: E402
    _take_prewarmed_rec, _build_rec_cmd, calibrate_noise,
    is_speech_energy, rms_energy, _schedule_warmup, _prewarm_recorder,
    _discard_prewarmed_rec,
    detect_audio_output, has_echo_cancel, _refresh_audio_detection,
)
from stt import (  # noqa: E402
    stt as stt_dispatch,
    _get_stt_ws, _invalidate_stt_ws, _init_stt_ws_session,
    _make_ws_audio_msg, _parse_ws_msg, _rest_stt_fallback,
    _check_end_word, _strip_end_word,
)
import speech_tts  # noqa: E402

if HAS_VAD:
    import webrtcvad  # noqa: E402
if HAS_WS:
    import websocket  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    stream=sys.stderr,
    level=logging.DEBUG,
    format="%(asctime)s [gnome-speaks] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gnome-speaks")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUS_NAME = "org.gnome.Speaks"
OBJECT_PATH = "/org/gnome/Speaks"
INTERFACE_NAME = "org.gnome.Speaks"

INACTIVITY_TIMEOUT_SEC = int(
    os.environ.get("GNOME_SPEAKS_INACTIVITY_TIMEOUT_SEC", "600")
)  # 10 minutes default; set to 0 to disable (e.g. when the HTTP API must stay reachable)

MAX_LISTEN_SECONDS = 30

# ---------------------------------------------------------------------------
# DBus introspection XML
# ---------------------------------------------------------------------------

INTROSPECTION_XML = """
<node>
  <interface name="org.gnome.Speaks">
    <method name="StartListening">
      <arg direction="out" type="s" name="result"/>
    </method>
    <method name="StopListening">
      <arg direction="out" type="s" name="transcription"/>
    </method>
    <method name="Speak">
      <arg direction="in" type="s" name="text"/>
      <arg direction="out" type="b" name="success"/>
    </method>
    <method name="SpeakClipboard">
      <arg direction="out" type="b" name="success"/>
    </method>
    <method name="SpeakSelection">
      <arg direction="out" type="b" name="success"/>
    </method>
    <method name="SetLanguage">
      <arg direction="in" type="s" name="language"/>
      <arg direction="out" type="b" name="success"/>
    </method>
    <method name="GetLanguage">
      <arg direction="out" type="s" name="language"/>
    </method>
    <method name="ToggleConversationMode">
      <arg direction="out" type="b" name="enabled"/>
    </method>
    <method name="ToggleContinuousDictation">
      <arg direction="out" type="b" name="enabled"/>
    </method>
    <method name="ToggleVoiceQuality">
      <arg direction="out" type="s" name="quality"/>
    </method>
    <method name="GetVoiceQuality">
      <arg direction="out" type="s" name="quality"/>
    </method>
    <method name="ToggleBargeIn">
      <arg direction="out" type="b" name="enabled"/>
    </method>
    <method name="GetBargeIn">
      <arg direction="out" type="b" name="enabled"/>
    </method>
    <method name="ToggleHandsFree">
      <arg direction="out" type="b" name="enabled"/>
    </method>
    <method name="GetContinuousDictation">
      <arg direction="out" type="b" name="enabled"/>
    </method>
    <method name="GetConversationMode">
      <arg direction="out" type="b" name="enabled"/>
    </method>
    <method name="GetHandsFree">
      <arg direction="out" type="b" name="enabled"/>
    </method>
    <method name="ToggleTerminalMode">
      <arg direction="out" type="b" name="enabled"/>
    </method>
    <method name="GetTerminalMode">
      <arg direction="out" type="b" name="enabled"/>
    </method>
    <method name="Talk">
      <arg direction="in" type="s" name="text"/>
      <arg direction="out" type="s" name="reply"/>
    </method>
    <method name="GetAudioInfo">
      <arg direction="out" type="s" name="info"/>
    </method>
    <method name="SetSTTMode">
      <arg direction="in" type="s" name="mode"/>
      <arg direction="out" type="b" name="success"/>
    </method>
    <method name="GetSTTMode">
      <arg direction="out" type="s" name="mode"/>
    </method>
    <method name="GetSTTModes">
      <arg direction="out" type="s" name="modes"/>
    </method>
    <method name="PlaySound">
      <arg direction="in" type="s" name="sound_name"/>
      <arg direction="out" type="b" name="success"/>
    </method>
    <method name="Stop">
      <arg direction="out" type="b" name="success"/>
    </method>
    <method name="GetState">
      <arg direction="out" type="s" name="state"/>
    </method>
    <method name="GetChronicle">
      <arg direction="in" type="i" name="limit"/>
      <arg direction="out" type="s" name="entries_json"/>
    </method>
    <method name="Respeak">
      <arg direction="in" type="x" name="id"/>
      <arg direction="out" type="b" name="success"/>
    </method>
    <signal name="StateChanged">
      <arg type="s" name="state"/>
    </signal>
    <signal name="TranscriptionReady">
      <arg type="s" name="text"/>
    </signal>
    <signal name="PartialTranscription">
      <arg type="s" name="text"/>
    </signal>
    <signal name="SubtitleUpdate">
      <arg type="s" name="text"/>
      <arg type="d" name="duration"/>
      <arg type="i" name="percent"/>
    </signal>
    <signal name="AudioLevel">
      <arg type="d" name="level"/>
    </signal>
    <signal name="STTStatus">
      <arg type="b" name="speech_detected"/>
      <arg type="d" name="timeout_fraction"/>
    </signal>
    <signal name="Error">
      <arg type="s" name="message"/>
    </signal>
  </interface>
</node>
"""

# ---------------------------------------------------------------------------
# The Chronicle — append-only ledger of everything said (both directions).
# One JSON object per line; local-only (never enters git). Entries are
# respeakable via POST /respeak, the Respeak D-Bus method, or "cast echo".
# ---------------------------------------------------------------------------

CHRONICLE_PATH = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "gnome-speaks", "chronicle.jsonl")
_chronicle_lock = threading.Lock()
_chronicle_last_id = 0


def _chronicle_append(kind, text, voice=None, source=None):
    """Record one utterance. kind: 'you' (heard) or 'spoken' (played).

    Best-effort — a full disk must never take down the voice pipeline.
    """
    if not CONFIG.get("chronicle", True):
        return
    text = (text or "").strip()
    if not text:
        return
    global _chronicle_last_id
    entry = {"kind": kind, "text": text,
             "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    if voice:
        entry["voice"] = voice
    if source:
        entry["source"] = source
    try:
        with _chronicle_lock:
            # ms timestamp as id; bump on same-ms collisions so ids stay
            # unique without a persistent counter.
            eid = int(time.time() * 1000)
            if eid <= _chronicle_last_id:
                eid = _chronicle_last_id + 1
            _chronicle_last_id = eid
            entry["id"] = eid
            os.makedirs(os.path.dirname(CHRONICLE_PATH), exist_ok=True)
            with open(CHRONICLE_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        log.warning("Chronicle append failed", exc_info=True)


def _chronicle_read(limit=20, q=None, kind=None):
    """Return the newest matching entries, oldest-first (ready to display)."""
    entries = deque(maxlen=max(1, min(int(limit or 20), 500)))
    try:
        with _chronicle_lock, open(CHRONICLE_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if kind and entry.get("kind") != kind:
                    continue
                if q and q.lower() not in entry.get("text", "").lower():
                    continue
                entries.append(entry)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("Chronicle read failed", exc_info=True)
    return list(entries)


def _chronicle_find(entry_id):
    """Return the entry with this id, or None."""
    try:
        with _chronicle_lock, open(CHRONICLE_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("id") == entry_id:
                    return entry
    except (FileNotFoundError, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Clipboard helpers (our own — do not import from speech-to-cli)
# ---------------------------------------------------------------------------

def clipboard_read():
    """Read text from the clipboard (Wayland-first, X11 fallback)."""
    for cmd in [["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"]]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return result.stdout
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            log.debug("Clipboard read timed out: %s", cmd[0])
    return ""


def clipboard_write(text):
    """Write text to the clipboard (Wayland-first, X11 fallback)."""
    for cmd in [["wl-copy"], ["xclip", "-selection", "clipboard"]]:
        try:
            subprocess.run(
                cmd, input=text.encode("utf-8"),
                check=True, timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except FileNotFoundError:
            continue
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            log.debug("Clipboard write failed with %s: %s", cmd[0], exc)
    return False


_TYPING_TOOL = None
_YDOTOOL_V1 = False  # True if ydotool >= 1.0 (daemon mode, different CLI flags)
_typing_tool_detected = False


def _detect_typing_tool():
    """Detect the best available typing tool. Lazy — runs once on first call."""
    global _typing_tool_detected
    if _typing_tool_detected:
        return
    _typing_tool_detected = True
    global _TYPING_TOOL, _YDOTOOL_V1
    if shutil.which("ydotool"):
        _TYPING_TOOL = "ydotool"
        _YDOTOOL_V1 = shutil.which("ydotoold") is not None or _is_ydotoold_running()
        if _YDOTOOL_V1:
            log.info("Typing tool: ydotool v1.0+ (daemon mode)")
        else:
            log.info("Typing tool: ydotool v0.x (no daemon)")
    elif shutil.which("xdotool"):
        _TYPING_TOOL = "xdotool"
        log.info("Typing tool: xdotool")
    else:
        _TYPING_TOOL = "clipboard"
        log.info("Typing tool: clipboard (install ydotool for live typing)")


def _is_ydotoold_running():
    """Check if ydotoold daemon is running."""
    try:
        result = subprocess.run(["pidof", "ydotoold"], capture_output=True, timeout=2)
        return result.returncode == 0
    except Exception as exc:
        log.debug("ydotoold check failed: %s", exc)
        return False


_ydotool_reset_lock = threading.Lock()


def _reset_ydotoold():
    """Restart ydotoold to clear any stuck key state on its virtual device.

    When a ydotool command is interrupted between a key-down and key-up event,
    the virtual uinput device retains that key as "pressed". The Wayland
    compositor then suppresses that key from all physical keyboards. Restarting
    the daemon destroys the old virtual device and creates a clean one.

    Uses a lock to prevent two threads from restarting simultaneously.
    """
    if not _YDOTOOL_V1:
        return
    if not _ydotool_reset_lock.acquire(blocking=False):
        return  # another thread is already restarting
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", "ydotoold"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5,
        )
        # Give ydotoold time to create the new virtual device
        time.sleep(0.1)
        log.info("Reset ydotoold to clear stuck key state")
    except Exception as e:
        log.warning("Failed to restart ydotoold: %s", e)
    finally:
        _ydotool_reset_lock.release()


def _run_ydotool(args, **kwargs):
    """Run a ydotool command, resetting ydotoold if it fails or times out.

    Any interrupted ydotool key/type command can leave keys stuck on the
    virtual device. This wrapper catches failures and restarts the daemon
    to prevent permanent key loss.
    """
    try:
        subprocess.run(args, **kwargs)
    except subprocess.TimeoutExpired:
        log.warning("ydotool timed out (%s), resetting ydotoold", args[1])
        _reset_ydotoold()
    except Exception as e:
        log.warning("ydotool failed (%s): %s, resetting ydotoold", args[1], e)
        _reset_ydotoold()


def _send_backspaces(count):
    """Send N backspace keypresses via ydotool or xdotool. Blocks until complete."""
    if count <= 0:
        return
    if _TYPING_TOOL == "ydotool":
        if _YDOTOOL_V1:
            # v1.0+: -d for key-delay, repeat by listing key pairs N times
            # Listing all pairs is more reliable than --repeat which doesn't exist in v1
            keys = ["14:1", "14:0"] * count
            _run_ydotool(
                ["ydotool", "key", "-d", "1"] + keys,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5,
            )
        else:
            # v0.1.x: --delay for device registration, --key-delay, --repeat
            _run_ydotool(
                ["ydotool", "key", "--delay", "50", "--key-delay", "0",
                 "--repeat", str(count), "14:1", "14:0"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5,
            )
    elif _TYPING_TOOL == "xdotool" and os.environ.get("DISPLAY"):
        subprocess.run(
            ["xdotool", "key", "--clearmodifiers"] + ["BackSpace"] * count,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5,
        )


def _type_raw(text):
    """Type text at the cursor. Blocks until complete."""
    if not text:
        return
    if _TYPING_TOOL == "ydotool":
        if _YDOTOOL_V1:
            # v1.0+: pipe text via stdin to avoid argument-parsing space issues.
            # 12ms key delay — sweet spot for reliable terminal input without
            # feeling sluggish (~83 chars/sec). Lower values drop characters.
            _run_ydotool(
                ["ydotool", "type", "-d", "12", "--file", "-"],
                input=text.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=15,
            )
        else:
            # v0.1.x: --delay for device registration, --key-delay between chars
            _run_ydotool(
                ["ydotool", "type", "--delay", "50", "--key-delay", "0", "--", text],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
    elif _TYPING_TOOL == "xdotool" and os.environ.get("DISPLAY"):
        subprocess.run(
            ["xdotool", "type", "--clearmodifiers", "--", text],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10,
        )


def type_at_cursor(text):
    """Type text at the current cursor position using the pre-detected tool."""
    if not text:
        return False

    # Brief delay to let focus settle after badge interaction
    time.sleep(0.05)

    _type_raw(text)
    if _TYPING_TOOL in ("ydotool", "xdotool"):
        return True

    log.warning("No typing tool available (install ydotool), copying to clipboard instead")
    return clipboard_write(text)


def _clipboard_paste(text):
    """Copy text to clipboard and paste via Ctrl+Shift+V (terminals) or Ctrl+V."""
    try:
        subprocess.run(["wl-copy", "--", text], timeout=2,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        log.warning("Clipboard paste failed: %s", exc)
        return False
    time.sleep(0.01)
    if _TYPING_TOOL == "ydotool":
        if _YDOTOOL_V1:
            # Ctrl(29) + Shift(42) + V(47) — works in terminals and most apps
            _run_ydotool(
                ["ydotool", "key", "-d", "3",
                 "29:1", "42:1", "47:1", "47:0", "42:0", "29:0"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5,
            )
        else:
            _run_ydotool(
                ["ydotool", "key", "--delay", "50", "--key-delay", "3",
                 "29:1", "42:1", "47:1", "47:0", "42:0", "29:0"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5,
            )
    return True


def replace_typed_text(old_text, new_text):
    """Update live-typed text. Append-only when possible to avoid visual flicker.

    If new_text is an extension of old_text, just type the new suffix (fast, smooth).
    If it's a revision, only backspace the changed tail — find the common prefix
    and only delete/retype from where the texts diverge.
    """
    if _TYPING_TOOL not in ("ydotool", "xdotool"):
        return  # clipboard mode can't do live partials
    if old_text == new_text:
        return
    # Append-only: Azure hypotheses almost always extend the previous one
    if new_text.startswith(old_text):
        suffix = new_text[len(old_text):]
        if suffix:
            _type_raw(suffix)
    else:
        # Hypothesis revised — find common prefix, only backspace the changed tail.
        # This minimizes visual disruption (e.g., "hello how our" → "hello how are"
        # only backspaces 3 chars and types 3, not the full 13+13).
        common = 0
        while common < len(old_text) and common < len(new_text) and old_text[common] == new_text[common]:
            common += 1
        chars_to_delete = len(old_text) - common
        new_suffix = new_text[common:]
        if chars_to_delete > 0:
            _send_backspaces(chars_to_delete)
            time.sleep(0.01)
        if new_suffix:
            # Use clipboard paste for longer corrections (>15 chars) —
            # ydotool can drop characters in longer bursts.
            # Short suffixes use direct typing for lower latency.
            if len(new_suffix) > 15:
                _clipboard_paste(new_suffix)
            else:
                _type_raw(new_suffix)


# ── Terminal-mode smart lowercasing ──────────────────────────────────────
# Lowercase by default but preserve correct casing for filesystem entries.
# Caches directory listings for 30s to avoid repeated disk I/O.

_fs_case_cache = {}   # {lowercase_name: correct_name}
_fs_case_time = 0.0

def _refresh_fs_case_cache():
    """Rebuild the case-correction lookup from common directories."""
    global _fs_case_cache, _fs_case_time
    now = time.monotonic()
    if now - _fs_case_time < 30.0:
        return
    _fs_case_time = now
    cache = {}
    for d in [os.path.expanduser("~/Projects"), os.path.expanduser("~"),
              "/etc", "/var", "/tmp"]:
        try:
            for name in os.listdir(d):
                cache[name.lower()] = name
        except OSError:
            pass
    # Also include configured phrase_list entries
    for phrase in CONFIG.get("phrase_list", []):
        for word in phrase.split():
            cache[word.lower()] = word
    _fs_case_cache = cache


def _terminal_lowercase(text):
    """Lowercase text but preserve casing for known filesystem entries and phrases."""
    _refresh_fs_case_cache()
    words = text.split()
    result = []
    for w in words:
        low = w.lower()
        # Check if this word matches a known filesystem entry or phrase
        corrected = _fs_case_cache.get(low)
        result.append(corrected if corrected else low)
    return " ".join(result)


def selection_read():
    """Read the currently highlighted/selected text (PRIMARY selection)."""
    for cmd in [["wl-paste", "--primary", "--no-newline"], ["xclip", "-selection", "primary", "-o"]]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return result.stdout
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            log.debug("Selection read timed out: %s", cmd[0])
    return ""


# Voice commands: spoken punctuation → actual characters
_VOICE_COMMANDS = [
    (re.compile(r'\b(?:period|full stop)\b', re.I), '.'),
    (re.compile(r'\bcomma\b', re.I), ','),
    (re.compile(r'\bquestion mark\b', re.I), '?'),
    (re.compile(r'\bexclamation (?:mark|point)\b', re.I), '!'),
    (re.compile(r'\bcolon\b', re.I), ':'),
    (re.compile(r'\bsemicolon\b', re.I), ';'),
    (re.compile(r'\bnew line\b', re.I), '\n'),
    (re.compile(r'\bnew paragraph\b', re.I), '\n\n'),
    (re.compile(r'\bopen quote\b', re.I), '"'),
    (re.compile(r'\bclose quote\b', re.I), '"'),
    (re.compile(r'\bhyphen\b', re.I), '-'),
    (re.compile(r'\bdash\b', re.I), ' \u2014 '),
    (re.compile(r'\bellipsis\b', re.I), '...'),
    (re.compile(r'\btab key\b', re.I), '\t'),
]


def apply_voice_commands(text):
    """Replace spoken punctuation commands with actual characters."""
    if not CONFIG.get("voice_commands", True):
        return text
    result = text
    for pattern, replacement in _VOICE_COMMANDS:
        result = pattern.sub(replacement, result)
    # Clean up whitespace before punctuation
    result = re.sub(r'\s+([.,?!:;])', r'\1', result)
    return result


_SERVICE_START_TIME = time.time()
_SERVICE_START_ISO = time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _get_ha_token():
    """Home Assistant long-lived token: env → cache file → Vaultwarden.
    Returns None when unavailable. The value is never logged."""
    token = os.environ.get("HA_TOKEN", "").strip()
    if token:
        return token
    try:
        with open(os.path.expanduser("~/.cache/ha-token-tmp")) as f:
            token = f.read().strip()
        if token:
            return token
    except OSError:
        pass
    try:
        proc = subprocess.run(["bw", "get", "password", "ha-llat"],
                              capture_output=True, text=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def apply_auto_corrections(text):
    """Apply user-defined word corrections from config."""
    corrections = CONFIG.get("auto_corrections", {})
    if not corrections:
        return text
    for wrong, right in corrections.items():
        text = re.sub(r'\b' + re.escape(wrong) + r'\b', right, text, flags=re.IGNORECASE)
    return text


# ---------------------------------------------------------------------------
# Service implementation
# ---------------------------------------------------------------------------

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
    source: str | None = None       # coalescing key: "only my latest matters"


class GnomeSpeaksService:
    """Core service logic using speech-to-cli building blocks."""

    STATES = ("idle", "listening", "processing", "speaking")

    def __init__(self):
        self._state = "idle"
        self._state_lock = threading.Lock()

        # STT streaming state
        self._stop_event = threading.Event()  # our own, NOT state._cancel_event
        self._stt_thread = None
        self._stt_lock = threading.Lock()

        # TTS state
        self._speak_thread = None
        self._speak_lock = threading.Lock()

        # Talk (full-duplex TTS+STT) state
        self._talk_thread = None
        self._talk_lock = threading.Lock()

        # STT mode selection (auto, streaming, whisper, vad, fixed)
        self._stt_mode = "auto"

        # Inactivity timer
        self._inactivity_source_id = None
        self._main_loop = None

        # DBus connection (set after registration)
        self._connection = None

        # Voice quality toggle (hd = DragonHD + eastus, fast = Neural + westus)
        self._voice_quality = "fast"
        self._original_tts_region = CONFIG.get("tts_region")
        self._original_tts_key = CONFIG.get("tts_key")

        # Conversation history (cleared when conversation mode toggled off)
        self._conversation_history = []
        self._conversation_lock = threading.Lock()

        # Partial transcription throttling (FIX 4)
        self._last_partial_time = 0
        self._last_partial_text = ""

        # Config file mtime cache (FIX 8)
        self._config_mtime = 0

        # Audio detection flag (FIX 12)
        self._audio_detected = False

        # HTTP progress tracking for REST API status endpoint
        self._http_progress = {
            "text": "", "elapsed": 0.0, "estimated_duration": 0.0,
            "percent": 0, "started_at": 0.0,
            "pause_accumulated": 0.0, "pause_started": 0.0,
        }
        self._http_progress_lock = threading.Lock()

        # HTTP speech queue — agents on :7710 queue FIFO instead of stomping
        # each other. User speech paths set _user_speech_active to hold the
        # dispatcher (user outranks agents).
        self._tts_queue = queue.Queue(maxsize=32)
        self._tts_queue_seq = itertools.count(1)  # next() is atomic in CPython
        self._queue_current = None                # TTSQueueItem now playing
        self._queue_current_lock = threading.Lock()
        self._user_speech_active = threading.Event()
        # Playback ownership token: each playback claims a fresh object();
        # a preempted worker's cleanup must not reset state it no longer owns.
        self._speak_token = None
        # Terminal outcome per queue item (done/canceled/interrupted/error),
        # exposed on GET /queue so the /speak id isn't write-only. Exactly one
        # terminal outcome per item (Android-style invariant).
        self._queue_recent = deque(maxlen=16)
        # Serializes coalesce-then-put so two concurrent bursts from the same
        # source can't interleave (drop, drop, put, put would leave two items
        # where the caller asked for one). Order is always _enqueue_lock ->
        # _tts_queue.mutex; never the reverse.
        self._enqueue_lock = threading.Lock()
        threading.Thread(target=self._tts_dispatcher, daemon=True,
                         name="tts-queue-dispatcher").start()

        # Voice spellbook (incantation layer) — "cast …" routes here instead
        # of typing/LLM. Repo default + user overlay, mtime hot-reload.
        self._spellbook_paths = (
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "spellbook.json"),
            os.path.expanduser("~/.config/speech-to-cli/spellbook.json"),
        )
        self._spellbook = spellbook.load_spellbook(*self._spellbook_paths)
        self._spellbook_mtimes = self._spellbook_stat()
        self._spell_executor = spellbook.SpellExecutor(
            chime=self.play_sound, speak=self._spell_speak,
            dbus_self=self._spell_ctx_dbus, confirm=self.talk,
            ha_token=_get_ha_token)

        # Wake word watcher (idle-only; no-op until wake_word is enabled)
        threading.Thread(target=self._wake_watcher, daemon=True,
                         name="wake-watcher").start()

    # -- Config sync -------------------------------------------------------

    # Boolean flags that prefs.js can change on disk while the service runs.
    _SYNC_FLAGS = (
        # Mode flags
        "conversation_mode", "continuous_dictation", "dictation_mode",
        "terminal_mode", "skip_final_paste", "read_notifications",
        "wake_word", "wake_word_model", "llm_thinking",
        "speed", "pitch", "volume", "chronicle",
        # LLM provider config
        "llm_provider", "llm_model", "llm_api_key", "llm_system_prompt",
        # Chimes
        "chime_ready", "chime_processing", "chime_speak", "chime_done",
        "chime_hum", "chime_barge_in",
        # TTS voice settings (read per-call by speech_tts.py)
        "voice", "fast_voice",
        # STT / timing
        "language", "end_word", "voice_commands",
        "silence_timeout", "no_speech_timeout", "loop_silence_timeout",
        "conversation_silence_timeout", "talk_silence_timeout",
        "max_record_seconds",
        # Barge-in
        "enable_barge_in", "barge_in_frames", "barge_in_silence",
        # Visual feedback toggles
        "show_waveform", "show_vad_dot", "show_silence_fade",
        "show_badge_pulse", "show_badge_scale", "show_word_highlights",
        # Debug
        "debug",
    )

    def _reload_config_flags(self):
        """Re-read boolean mode flags from config file so prefs changes take effect.

        Skips JSON parse if file mtime is unchanged since last read.
        """
        path = os.path.expanduser("~/.config/speech-to-cli/config.json")
        try:
            mtime = os.path.getmtime(path)
            if mtime == self._config_mtime:
                return
            self._config_mtime = mtime
            with open(path) as f:
                disk = json.load(f)
            for key in self._SYNC_FLAGS:
                if key in disk:
                    CONFIG[key] = disk[key]
        except Exception as exc:
            log.debug("Config reload skipped: %s", exc)

    def _save_config_flag(self, key, value):
        """Write a single flag back to the config file so prefs stays in sync."""
        CONFIG[key] = value
        try:
            path = os.path.expanduser("~/.config/speech-to-cli/config.json")
            with open(path) as f:
                disk = json.load(f)
            disk[key] = value
            with open(path, "w") as f:
                json.dump(disk, f, indent=2)
        except Exception as e:
            log.warning("Failed to save config flag %s: %s", key, e)

    # -- State management --------------------------------------------------

    @property
    def current_state(self):
        with self._state_lock:
            return self._state

    _VALID_TRANSITIONS = {
        "idle": {"listening", "speaking"},
        "listening": {"processing", "idle"},
        "speaking": {"idle", "listening"},  # listening: hands-free auto-restart
        "processing": {"idle", "speaking"},
    }

    def _set_state(self, new_state):
        """Set state and emit StateChanged on the main loop.

        Signal emission is queued inside the lock to prevent another thread
        from changing state between the assignment and the GLib.idle_add.
        """
        with self._state_lock:
            if self._state == new_state:
                return
            allowed = self._VALID_TRANSITIONS.get(self._state, set())
            if new_state not in allowed:
                log.warning("Unexpected transition %s -> %s (forcing)", self._state, new_state)
            old = self._state
            self._state = new_state
            GLib.idle_add(self._emit_state_changed, new_state)
        log.info("State %s -> %s", old, new_state)
        self._reset_inactivity_timer()

    def _emit_state_changed(self, state_str):
        if self._connection is not None:
            self._connection.emit_signal(
                None, OBJECT_PATH, INTERFACE_NAME,
                "StateChanged",
                GLib.Variant("(s)", (state_str,)),
            )
        return False

    def _emit_transcription_ready(self, text):
        _chronicle_append("you", text)
        if self._connection is not None:
            self._connection.emit_signal(
                None, OBJECT_PATH, INTERFACE_NAME,
                "TranscriptionReady",
                GLib.Variant("(s)", (text,)),
            )
        return False

    def _emit_partial_transcription(self, text):
        if self._connection is not None:
            self._connection.emit_signal(
                None, OBJECT_PATH, INTERFACE_NAME,
                "PartialTranscription",
                GLib.Variant("(s)", (text,)),
            )
        return False

    def _throttled_partial_transcription(self, text):
        """Throttle partial transcription D-Bus signals during STT.

        Only emits if text has changed AND at least 150ms elapsed since last emit.
        Called from worker threads — schedules via GLib.idle_add when emitting.
        """
        now = time.monotonic()
        if text == self._last_partial_text:
            return
        if (now - self._last_partial_time) < 0.15:
            return
        self._last_partial_text = text
        self._last_partial_time = now
        GLib.idle_add(self._emit_partial_transcription, text)

    def _emit_subtitle_update(self, text, duration, percent):
        if self._connection is not None:
            self._connection.emit_signal(
                None, OBJECT_PATH, INTERFACE_NAME,
                "SubtitleUpdate",
                GLib.Variant("(sdi)", (text, duration, percent)),
            )
        return False

    def _emit_audio_level(self, level):
        if self._connection is not None:
            self._connection.emit_signal(
                None, OBJECT_PATH, INTERFACE_NAME,
                "AudioLevel",
                GLib.Variant("(d)", (level,)),
            )
        return False

    def _emit_stt_status(self, speech_detected, timeout_fraction):
        if self._connection is not None:
            self._connection.emit_signal(
                None, OBJECT_PATH, INTERFACE_NAME,
                "STTStatus",
                GLib.Variant("(bd)", (speech_detected, timeout_fraction)),
            )
        return False

    def _emit_error(self, message):
        log.error("Error signal: %s", message)
        if self._connection is not None:
            self._connection.emit_signal(
                None, OBJECT_PATH, INTERFACE_NAME,
                "Error",
                GLib.Variant("(s)", (message,)),
            )
        return False

    # -- Inactivity timer --------------------------------------------------

    def _reset_inactivity_timer(self):
        if self._inactivity_source_id is not None:
            GLib.source_remove(self._inactivity_source_id)
            self._inactivity_source_id = None
        if INACTIVITY_TIMEOUT_SEC <= 0:
            return
        self._inactivity_source_id = GLib.timeout_add_seconds(
            INACTIVITY_TIMEOUT_SEC, self._on_inactivity_timeout,
        )

    def _on_inactivity_timeout(self):
        if self.current_state == "idle":
            log.info("Inactivity timeout reached, quitting.")
            if self._main_loop is not None:
                self._main_loop.quit()
            return False
        self._inactivity_source_id = None
        self._reset_inactivity_timer()
        return False

    # -- STT: Streaming WebSocket using speech-to-cli building blocks ------

    def start_listening(self, quick=False):
        """Start microphone recording with STT. Returns 'ok' or error string.

        If quick=True, skip config reload and audio detection refresh.
        Used for tight loop restarts where config hasn't changed.
        """
        # Prevent concurrent STT threads from rapid clicks.
        # For quick (loop) restarts, briefly wait for the old thread to finish
        # since the loop restart fires before the thread fully exits.
        if hasattr(self, '_stt_thread') and self._stt_thread and self._stt_thread.is_alive():
            if quick:
                self._stt_thread.join(timeout=1.0)
            if self._stt_thread and self._stt_thread.is_alive():
                log.warning("STT thread already running, ignoring start_listening")
                return "error: STT thread already running"

        if not quick:
            self._reload_config_flags()
            if not self._audio_detected:
                _refresh_audio_detection()
                self._audio_detected = True
        if self.current_state != "idle":
            return f"error: busy ({self.current_state})"

        # Determine effective mode
        mode = self._stt_mode
        if mode == "auto":
            if HAS_WS and HAS_VAD:
                mode = "streaming"
            elif HAS_VAD:
                mode = "vad"
            else:
                mode = "fixed"

        # Use non-streaming STT backends (whisper, vad, fixed)
        if mode in ("whisper", "vad", "fixed"):
            if mode == "whisper" and not HAS_WHISPER:
                GLib.idle_add(self._emit_error, "faster-whisper not installed")
                return "error: no whisper support"
            if mode != "whisper" and not CONFIG.get("key"):
                GLib.idle_add(self._emit_error, "Azure Speech key not configured")
                return "error: no API key"

            self._stop_event.clear()
            self._set_state("listening")

            self._stt_thread = threading.Thread(
                target=self._batch_stt_worker,
                args=(mode,),
                daemon=True,
            )
            self._stt_thread.start()
            return "ok"

        # Streaming mode (default)
        if not CONFIG.get("key"):
            GLib.idle_add(self._emit_error, "Azure Speech key not configured")
            return "error: no API key"

        if not HAS_WS:
            GLib.idle_add(self._emit_error, "websocket-client not installed")
            return "error: no websocket support"

        self._stop_event.clear()
        self._set_state("listening")

        self._stt_thread = threading.Thread(
            target=self._streaming_stt_worker,
            daemon=True,
        )
        self._stt_thread.start()
        return "ok"

    def _batch_stt_worker(self, mode):
        """Background thread: batch STT using stt() dispatcher (whisper, vad, fixed)."""
        try:
            state._cancel_event.clear()
            result = stt_dispatch(mode=mode)

            if result.get("cancelled"):
                log.info("STT cancelled")
                GLib.idle_add(self._emit_transcription_ready, "")
                self._set_state("idle")
                _schedule_warmup()
                return

            user_text = result.get("text", "")

            self._set_state("processing")

            # Spell incantations ("cast …") short-circuit typing/LLM routing;
            # matched on the raw transcript before punctuation substitution.
            if user_text and self._try_cast(user_text):
                GLib.idle_add(self._emit_transcription_ready, user_text)
                self._set_state("idle")
                _schedule_warmup()
                return

            if user_text:
                user_text = apply_voice_commands(user_text)
                user_text = apply_auto_corrections(user_text)
                GLib.idle_add(self._emit_transcription_ready, user_text)
                log.info("Transcription (%s): %s", mode, user_text[:100])

                if CONFIG.get("conversation_mode", False):
                    self._conversation_worker(user_text)
                    # One-shot: turn off after AI responds
                    if not CONFIG.get("continuous_dictation", False):
                        self._save_config_flag("conversation_mode", False)
                    # Warmup + restart already handled inside _conversation_worker
                    return

                if CONFIG.get("dictation_mode", True):
                    type_at_cursor(user_text)
                else:
                    clipboard_write(user_text)
            else:
                log.info("No speech detected (%s)", mode)
                GLib.idle_add(self._emit_transcription_ready, "")

            self._set_state("idle")
            _schedule_warmup()

            if user_text and CONFIG.get("continuous_dictation", False) and not self._stop_event.is_set():
                GLib.idle_add(lambda: (self.start_listening(quick=True), False)[-1]
                              if (CONFIG.get("continuous_dictation", False)
                                  and not self._stop_event.is_set())
                              else False)
        except Exception as exc:
            log.exception("Batch STT (%s) failed: %s", mode, exc)
            GLib.idle_add(self._emit_error, f"STT failed: {exc}")
            self._set_state("idle")
            _schedule_warmup()

    def _streaming_stt_worker(self):
        """Background thread: streaming STT using speech-to-cli building blocks.

        In continuous dictation (loop) mode, keeps the recorder process AND
        WebSocket session alive across multiple utterances — only the sender
        thread and per-cycle state are reset between cycles.  This eliminates
        WS session reinit (~50ms), recorder startup, and thread-creation
        overhead that the old start_listening(quick=True) path incurred.
        """
        _log_tag = "stt-gnome"
        _dbg = "/tmp/speech-debug.log" if (os.environ.get("SPEECH_DEBUG") or CONFIG.get("debug")) else None
        _DBG_MAX_SIZE = 5 * 1024 * 1024  # 5 MB

        def _log(msg):
            log.debug("STT: %s", msg)
            if _dbg:
                try:
                    if os.path.getsize(_dbg) > _DBG_MAX_SIZE:
                        os.rename(_dbg, _dbg + ".old")
                except OSError:
                    pass
                with open(_dbg, "a") as f:
                    f.write(f"[{_log_tag} {time.strftime('%H:%M:%S')}] {msg}\n")

        end_word = CONFIG.get("end_word", "over")
        is_loop = CONFIG.get("continuous_dictation", False)

        # 1. Get prewarmed recorder (or start fresh) — reused across all
        #    cycles in loop mode.
        proc = _take_prewarmed_rec()
        if proc is None:
            try:
                proc = subprocess.Popen(
                    _build_rec_cmd(),
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError as exc:
                GLib.idle_add(self._emit_error, f"Recorder not found: {exc}")
                self._set_state("idle")
                _schedule_warmup()
                return

        state.register_proc(proc)

        # 2. Get persistent WebSocket (with exponential backoff retry).
        ws = None
        ws_fresh = False
        _ws_max_attempts = 4
        _ws_backoff = 1.0  # seconds, doubles each attempt, caps at 30s
        for attempt in range(_ws_max_attempts):
            try:
                ws, ws_fresh = _get_stt_ws()
                break
            except Exception as exc:
                _log(f"WS connect attempt {attempt + 1}/{_ws_max_attempts} failed: {exc}")
                _invalidate_stt_ws()
                if attempt == _ws_max_attempts - 1:
                    proc.terminate()
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=1.0)
                    state.unregister_proc(proc)
                    GLib.idle_add(self._emit_error, f"STT WebSocket failed: {exc}")
                    self._set_state("idle")
                    _schedule_warmup()
                    return
                # Exponential backoff before next attempt
                delay = min(_ws_backoff, 30.0)
                _log(f"WS retry in {delay:.1f}s")
                time.sleep(delay)
                _ws_backoff *= 2

        # --- Mode flags (stable across cycles) ---
        live_typing = (CONFIG.get("dictation_mode", True)
                       and not CONFIG.get("conversation_mode", False)
                       and _TYPING_TOOL in ("ydotool", "xdotool"))
        use_lexical = CONFIG.get("terminal_mode", False)

        # ---------------------------------------------------------------
        # Main cycle loop — runs once in single-shot mode, loops in
        # continuous dictation mode.  Recorder and WS stay alive.
        # ---------------------------------------------------------------
        cycle = 0
        user_text = ""       # set each cycle; needed in cleanup for conversation_mode check
        natural_end = False  # set each cycle; needed in cleanup for single-shot restart
        while True:
            cycle += 1
            _log(f"=== cycle {cycle} (loop={is_loop}) ===")

            # 3. Init new WS session for this utterance
            request_id = uuid.uuid4().hex
            try:
                _init_stt_ws_session(ws, request_id, drain=not ws_fresh)
            except Exception as exc:
                _log(f"WS session init failed (cycle {cycle}): {exc}")
                _invalidate_stt_ws()
                # Try to reconnect once before giving up
                try:
                    ws, ws_fresh = _get_stt_ws()
                    _init_stt_ws_session(ws, request_id, drain=not ws_fresh)
                except Exception as exc2:
                    _log(f"WS reconnect also failed: {exc2}")
                    _invalidate_stt_ws()
                    GLib.idle_add(self._emit_error, f"STT session init failed: {exc2}")
                    break  # fall through to cleanup
            ws_fresh = False  # subsequent cycles always drain

            # 4. Per-cycle shared state — reset partial throttle for new utterance
            self._last_partial_text = ""
            self._last_partial_time = 0
            phrases = []
            partial_holder = [""]
            end_word_event = threading.Event()
            sender_done = threading.Event()
            raw_frames = []
            typed_partial = [""]
            raw_partial = [""]

            # 5. Sender thread: calibrate noise, send audio with VAD.
            #    A new sender thread is created each cycle, but the same proc
            #    (recorder) feeds it.  The sender does NOT terminate proc —
            #    that is handled by the outer cleanup below.
            def send_audio(_req_id=request_id, _raw_frames=raw_frames,
                           _end_word_event=end_word_event,
                           _sender_done=sender_done):
                try:
                    # Calibrate noise threshold (cached — reads only 1 frame after first call)
                    energy_threshold, cal_frames = calibrate_noise(proc)
                    _log(f"calibrated: threshold={energy_threshold:.0f}, cal_frames={len(cal_frames)}")

                    # Send buffered calibration frames to Azure
                    for frame in cal_frames:
                        ws.send(_make_ws_audio_msg(_req_id, frame), opcode=websocket.ABNF.OPCODE_BINARY)
                        _raw_frames.append(frame)

                    vad = webrtcvad.Vad(state.VAD_AGGRESSIVENESS) if HAS_VAD else None
                    silence_frames = 0
                    speech_frames = 0
                    total_frames = 0
                    # In loop mode, use tighter silence timeout for faster turnaround.
                    # Conversation mode gets a longer timeout (2.5s default) since
                    # natural speech has longer thinking pauses than dictation (1.2s).
                    if is_loop and CONFIG.get("conversation_mode", False):
                        silence_sec = CONFIG.get("conversation_silence_timeout", 2.5)
                    elif is_loop:
                        silence_sec = CONFIG.get("loop_silence_timeout", 1.2)
                    else:
                        silence_sec = state.SILENCE_TIMEOUT
                    max_silence = int(silence_sec * 1000 / FRAME_MS)
                    # In loop mode, wait much longer for speech before cycling.
                    # Default 7s causes ~8 restarts/min of silence, each with WS
                    # session re-init overhead. 60s keeps the session alive and
                    # responsive while burning near-zero resources in silence.
                    no_speech_sec = 60.0 if is_loop else state.NO_SPEECH_TIMEOUT
                    max_no_speech = int(no_speech_sec * 1000 / FRAME_MS)
                    min_speech = int(state.MIN_SPEECH_DURATION * 1000 / FRAME_MS)
                    max_frames = int(MAX_LISTEN_SECONDS * 1000 / FRAME_MS)

                    _log(f"limits: max_silence={max_silence} max_no_speech={max_no_speech} min_speech={min_speech}")

                    while not self._stop_event.is_set():
                        chunk = proc.stdout.read(FRAME_BYTES)
                        if not chunk or len(chunk) < FRAME_BYTES:
                            _log(f"recorder EOF at frame {total_frames}")
                            break

                        # Only buffer frames after speech starts (saves memory in loop idle)
                        if speech_frames > 0 or not is_loop:
                            _raw_frames.append(chunk)

                        try:
                            ws.send(_make_ws_audio_msg(_req_id, chunk), opcode=websocket.ABNF.OPCODE_BINARY)
                        except Exception as exc:
                            _log(f"WS send error at frame {total_frames}: {exc}")
                            break

                        energy = rms_energy(chunk)
                        total_frames += 1

                        is_speech = is_speech_energy(chunk, vad, energy_threshold)
                        if is_speech:
                            speech_frames += 1
                            silence_frames = 0
                        else:
                            silence_frames += 1

                        # Emit audio level for badge visualization (~90ms interval).
                        # In loop idle (no speech yet), throttle to every 27 frames
                        # (~270ms) to reduce D-Bus traffic while waiting.
                        emit_interval = 3 if speech_frames > 0 else 9
                        if total_frames % emit_interval == 0:
                            GLib.idle_add(self._emit_audio_level, min(energy / 10000.0, 1.0))
                            # STT status: VAD state + timeout progress
                            if speech_frames > 0:
                                # During speech: show silence countdown
                                tf = silence_frames / max_silence if max_silence > 0 else 0
                            else:
                                # Waiting for speech: show no-speech countdown
                                tf = total_frames / max_no_speech if max_no_speech > 0 else 0
                            # Also treat high energy as "speech" for visual feedback
                            # even if VAD hasn't confirmed — gives instant response
                            visual_speech = is_speech or (energy / 10000.0) > 0.15
                            GLib.idle_add(self._emit_stt_status, visual_speech, min(tf, 1.0))

                        if _end_word_event.is_set():
                            _log(f"STOP: end word '{end_word}' detected. speech={speech_frames}")
                            break
                        if speech_frames >= min_speech and silence_frames >= max_silence:
                            _log(f"STOP: silence timeout. speech={speech_frames} silence={silence_frames}/{max_silence}")
                            break
                        if speech_frames == 0 and total_frames >= max_no_speech:
                            _log(f"STOP: no speech timeout. total={total_frames}/{max_no_speech}")
                            break
                        if total_frames >= max_frames:
                            _log(f"STOP: max duration. total={total_frames}")
                            break

                    _log(f"REC END: speech={speech_frames} total={total_frames}")
                except Exception as exc:
                    _log(f"sender exception: {exc}")
                finally:
                    # Send end-of-audio marker for this utterance
                    try:
                        ws.send(_make_ws_audio_msg(_req_id, b""), opcode=websocket.ABNF.OPCODE_BINARY)
                    except Exception as exc:
                        _log(f"WS final audio send failed: {exc}")
                    # Do NOT terminate proc here — the outer loop handles cleanup.
                    _sender_done.set()

            sender = threading.Thread(target=send_audio, daemon=True)
            sender.start()

            # 6. Receive WS messages (on this thread)
            deadline = time.time() + MAX_LISTEN_SECONDS + 5
            got_phrase = False
            natural_end = False

            while time.time() < deadline and not self._stop_event.is_set():
                try:
                    ws.settimeout(1.0)
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    if sender_done.is_set():
                        if got_phrase:
                            break
                        try:
                            ws.settimeout(2.0)
                            msg = ws.recv()
                        except Exception as exc:
                            _log(f"WS recv after sender done: {exc}")
                            break
                    else:
                        continue
                except Exception as exc:
                    _log(f"WS recv error: {exc}")
                    break

                mtype = _parse_ws_msg(msg, phrases, partial_holder, end_word_event, end_word, _log,
                                      raw_partial_holder=raw_partial, use_lexical=use_lexical)

                if mtype == "hypothesis":
                    text = partial_holder[0]
                    if text:
                        self._throttled_partial_transcription(
                            _terminal_lowercase(text) if use_lexical else text)
                        if live_typing:
                            # Terminal mode: smart lowercase (preserves filesystem casing)
                            raw = _terminal_lowercase(raw_partial[0]) if use_lexical else raw_partial[0]
                            replace_typed_text(typed_partial[0], raw)
                            typed_partial[0] = raw
                elif mtype == "phrase":
                    got_phrase = True
                    text = partial_holder[0]
                    if text:
                        GLib.idle_add(self._emit_partial_transcription, text)
                    if sender_done.is_set():
                        try:
                            ws.settimeout(0.5)
                            ws.recv()  # drain final message
                        except Exception as exc:
                            _log(f"WS drain after phrase (expected): {exc}")
                        break
                elif mtype == "turn_end":
                    _log(f"turn.end received (phrases={len(phrases)})")
                    got_phrase = True
                    natural_end = True
                    # Signal sender to stop reading audio for this cycle.
                    # In loop mode we use a local flag instead of _stop_event
                    # so the outer loop can continue.
                    if is_loop:
                        end_word_event.set()  # reuse end_word_event to stop sender
                    else:
                        self._stop_event.set()
                    break

            # Wait for sender thread to finish this cycle
            if self._stop_event.is_set() and not sender_done.is_set():
                sender.join(timeout=2)
            elif not sender_done.is_set():
                sender.join(timeout=2)
            else:
                sender.join(timeout=0.5)

            # Drain any remaining WS messages after sender is done
            if sender_done.is_set():
                drain_deadline = time.time() + 1.0
                while time.time() < drain_deadline:
                    try:
                        ws.settimeout(0.5)
                        msg = ws.recv()
                    except Exception as exc:
                        _log(f"WS post-sender drain done: {exc}")
                        break
                    mtype = _parse_ws_msg(msg, phrases, partial_holder, end_word_event, end_word, _log,
                                          raw_partial_holder=raw_partial, use_lexical=use_lexical)
                    if mtype == "phrase":
                        got_phrase = True
                        text = partial_holder[0]
                        if text:
                            GLib.idle_add(self._emit_partial_transcription, text)
                    elif mtype == "turn_end":
                        got_phrase = True
                        break

            # 7. Final text
            user_text = " ".join(phrases).strip()

            if not user_text and raw_frames and not got_phrase:
                _log(f"WS returned nothing, falling back to REST STT (frames={len(raw_frames)})")
                user_text = _rest_stt_fallback(raw_frames, _log) or ""
            elif not user_text and got_phrase:
                _log(f"WS analyzed audio but found no speech (skipping REST fallback)")

            user_text = _strip_end_word(user_text, end_word)
            if use_lexical and user_text:
                user_text = _terminal_lowercase(user_text)
            _log(f"FINAL: {repr(user_text[:100])}")

            # Spell incantations ("cast …") short-circuit typing/LLM routing;
            # matched on the raw transcript before punctuation substitution.
            # In loop mode the cycle continues listening; replies queue until
            # the mic closes (never TTS over an open mic).
            if user_text and self._try_cast(user_text):
                if live_typing and typed_partial[0]:
                    _send_backspaces(len(typed_partial[0]))
                GLib.idle_add(self._emit_transcription_ready, user_text)
                if (is_loop and not self._stop_event.is_set()
                        and CONFIG.get("continuous_dictation", False)):
                    # Let the spell's spoken reply play before re-opening the mic
                    self._drain_speech_gap()
                    if self._stop_event.is_set():
                        break
                    self._set_state("listening")
                    continue
                break

            # 8. Post-process: voice commands and auto-corrections
            if user_text:
                user_text = apply_voice_commands(user_text)
                user_text = apply_auto_corrections(user_text)

            # 9. Emit results and type/copy
            # In loop mode, skip the "processing" flicker if nothing was said —
            # just silently re-enter listening on the next cycle.
            if is_loop and not user_text and not self._stop_event.is_set():
                if live_typing and typed_partial[0]:
                    _send_backspaces(len(typed_partial[0]))
                _log("no speech in loop cycle, continuing")
                # Quiet cycle = natural gap for starved queue items (agent
                # messages, spell replies) to play before the mic reopens.
                self._drain_speech_gap()
                self._set_state("listening")
                continue

            self._set_state("processing")
            if user_text:
                GLib.idle_add(self._emit_transcription_ready, user_text)
                log.info("Transcription: %s", user_text[:100])

                # Conversation mode: send to LLM then speak response
                if CONFIG.get("conversation_mode", False):
                    if live_typing:
                        _send_backspaces(len(typed_partial[0]))
                        time.sleep(0.02)
                    self._conversation_worker(user_text)
                    if not CONFIG.get("continuous_dictation", False):
                        self._save_config_flag("conversation_mode", False)
                    # _conversation_worker handles its own restart/warmup
                    break  # exit cycle loop; cleanup below

                # Type at cursor (dictation mode) or just copy to clipboard
                if CONFIG.get("dictation_mode", True):
                    if live_typing and (is_loop or CONFIG.get("skip_final_paste", False)):
                        if is_loop and typed_partial[0]:
                            # Final correction: if Azure's final differs from what
                            # was live-typed, surgically fix the divergent tail.
                            if typed_partial[0] != user_text:
                                replace_typed_text(typed_partial[0], user_text)
                            _type_raw(" ")
                    elif live_typing:
                        _send_backspaces(len(typed_partial[0]))
                        time.sleep(0.02)
                        _clipboard_paste(user_text)
                    else:
                        type_at_cursor(user_text)
                else:
                    if live_typing and typed_partial[0]:
                        _send_backspaces(len(typed_partial[0]))
                    clipboard_write(user_text)
            else:
                log.info("No speech detected")
                if live_typing and typed_partial[0]:
                    _send_backspaces(len(typed_partial[0]))
                GLib.idle_add(self._emit_transcription_ready, "")

            # 10. Decide whether to loop or exit
            if is_loop and not self._stop_event.is_set():
                # Re-check continuous_dictation in case user toggled it mid-session
                if not CONFIG.get("continuous_dictation", False):
                    _log("continuous_dictation toggled off, exiting loop")
                    break
                self._drain_speech_gap()
                # Reset state to "listening" for the next cycle
                self._set_state("listening")
                _log(f"cycle {cycle} done, continuing loop")
                continue

            # Single-shot mode or stop requested — exit
            break

        # ---------------------------------------------------------------
        # Cleanup: terminate recorder and set final state.
        # Only reached when exiting the cycle loop.
        # ---------------------------------------------------------------
        # Terminate the recorder process (it was kept alive across cycles)
        # pw-record ignores SIGTERM — escalate to SIGKILL after timeout
        try:
            proc.terminate()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                log.debug("Recorder ignored SIGTERM, sending SIGKILL")
                proc.kill()
                proc.wait(timeout=1.0)
        except Exception as exc:
            log.debug("Recorder cleanup error: %s", exc)
        state.unregister_proc(proc)

        # If we exited due to conversation_mode, it already set state + scheduled warmup
        if CONFIG.get("conversation_mode", False) and user_text:
            return

        self._set_state("idle")

        # If stop_event was set by turn_end (natural_end) in single-shot mode,
        # and continuous dictation is on, restart via start_listening (legacy path
        # for non-loop mode, e.g. conversation_mode toggled on mid-session).
        if not is_loop and CONFIG.get("continuous_dictation", False) and (natural_end or not self._stop_event.is_set()):
            if natural_end:
                self._stop_event.clear()
            self.start_listening(quick=True)
            return

        _schedule_warmup()

    def stop_listening(self):
        """Stop recording but keep accumulated text. Returns transcription."""
        if self.current_state != "listening":
            return ""

        # Signal our sender to stop (NOT state._cancel_event — that would kill the WS too)
        self._stop_event.set()

        # Wait for the STT worker thread to finish (it drains remaining WS messages)
        with self._stt_lock:
            thread = self._stt_thread
        if thread is not None:
            thread.join(timeout=10)
            with self._stt_lock:
                self._stt_thread = None

        # The worker thread already emitted TranscriptionReady and set state to idle.
        # Return empty here — the result was emitted via signal.
        return ""

    # -- TTS: Using speech_tts.tts() directly ------------------------------

    def enqueue_speech(self, text, voice=None, quality=None, output_file=None,
                       source=None, coalesce=False):
        """Create and enqueue an HTTP speech item for serial playback.

        Args:
            source: optional key identifying the producer, so it can coalesce
                its own backlog without touching anyone else's speech.
            coalesce: when True (requires source), drop this source's older
                UNSPOKEN items first — the newest status wins. The item that
                is currently PLAYING is never killed by coalescing; use /skip
                or interrupt for that.

        Returns (item_id, position, dropped_ids) where position is the number
        of items ahead (0 = will play next). Raises queue.Full at capacity.
        """
        item = TTSQueueItem(
            id=next(self._tts_queue_seq),
            text=text.strip(),
            voice=voice,
            quality=quality,
            output_file=output_file,
            enqueued_at=time.time(),
            source=source,
        )
        with self._enqueue_lock:
            dropped = (self._coalesce_source(source)
                       if coalesce and source else [])
            with self._queue_current_lock:
                busy = self._queue_current is not None
            position = self._tts_queue.qsize() + (1 if busy else 0)
            self._tts_queue.put_nowait(item)
        return item.id, position, dropped

    def respeak(self, entry_id=None):
        """Re-enqueue a chronicle entry for playback. None = newest 'spoken'.

        Returns the entry dict on success, None when nothing matches.
        A 'spoken' entry replays in its original voice; a 'you' entry is
        read back in the default voice.
        """
        if entry_id:
            entry = _chronicle_find(entry_id)
        else:
            last = _chronicle_read(limit=1, kind="spoken")
            entry = last[0] if last else None
        if not entry:
            return None
        try:
            self.enqueue_speech(entry["text"], voice=entry.get("voice"),
                                source="chronicle")
        except queue.Full:
            return None
        return entry

    def _coalesce_source(self, source):
        """Drop this source's not-yet-started items. Returns the dropped ids.

        Removes from the middle of the Queue, which has no public API for it,
        so we edit the underlying deque under the Queue's own mutex — the same
        access pattern GET /queue already uses for a snapshot. Safe because
        every producer here uses put_nowait (no blocked putter needs waking)
        and qsize()/maxsize read len(queue) live, so freed slots are real.
        (unfinished_tasks goes stale, which is inert: nothing calls
        task_done()/join() on this queue.)
        """
        dropped = []
        with self._tts_queue.mutex:
            keep = deque()
            for item in self._tts_queue.queue:
                if item.source == source:
                    dropped.append(item.id)
                else:
                    keep.append(item)
            if dropped:
                # clear()+extend() keeps the deque identity that Queue's own
                # methods close over — do not rebind self._tts_queue.queue.
                self._tts_queue.queue.clear()
                self._tts_queue.queue.extend(keep)
        for dropped_id in dropped:
            # Same terminal vocabulary as /stop's drain: never started.
            self._queue_recent.append({"id": dropped_id,
                                       "outcome": "canceled"})
        if dropped:
            log.info("Coalesced source %r: dropped %d unspoken item(s): ids %s",
                     source, len(dropped), dropped)
        return dropped

    def _drain_tts_queue(self):
        """Remove all pending speech-queue items. Returns the count cleared."""
        cleared = 0
        ids = []
        while True:
            try:
                item = self._tts_queue.get_nowait()
                ids.append(item.id)
                self._queue_recent.append({"id": item.id, "outcome": "canceled"})
                cleared += 1
            except queue.Empty:
                if cleared:
                    # Blast-radius trace: when two agents fight over the
                    # voice, this line is the whole debugging story.
                    log.info("Drained %d queued speech item(s): ids %s",
                             cleared, ids)
                return cleared

    def _tts_dispatcher(self):
        """Daemon thread: plays queued HTTP speech items serially.

        Holds while user speech is active or the mic/LLM is busy. Its own
        'speaking' state between back-to-back items does NOT hold it (the
        idle flap is suppressed via suppress_idle to avoid badge flicker).
        """
        while True:
            # Wait until clear BEFORE dequeuing, so held items stay visible
            # in GET /queue and remain drainable by /stop during user speech.
            # Pause is queue-level (unanimous across Web Speech/.NET/Apple):
            # a paused service must not start the next item either.
            while (self._user_speech_active.is_set()
                   or state._pause_event.is_set()
                   or self.current_state in ("listening", "processing")):
                time.sleep(0.2)
            try:
                item = self._tts_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            with self._queue_current_lock:
                self._queue_current = item
            try:
                if self.current_state != "speaking":
                    self._set_state("speaking")
                token = object()
                self._speak_token = token
                has_next = not self._tts_queue.empty()
                _chronicle_append("spoken", item.text, voice=item.voice,
                                  source=item.source or "queue")
                # Runs synchronously in this thread — playback is the wait.
                outcome = self._speak_worker(item.text, voice=item.voice,
                                             quality=item.quality,
                                             output_file=item.output_file,
                                             user_initiated=False,
                                             suppress_idle=has_next,
                                             owner_token=token)
                if state._cancel_event.is_set():
                    outcome = "interrupted"  # killed mid-play (skip/stop/preempt)
                self._queue_recent.append({"id": item.id,
                                           "outcome": outcome or "done"})
            except Exception:
                log.exception("Speech queue: item %d failed, continuing", item.id)
                self._queue_recent.append({"id": item.id, "outcome": "error"})
            finally:
                with self._queue_current_lock:
                    self._queue_current = None

    def speak(self, text, voice=None):
        """Synthesize and play text via speech_tts.tts(). Returns True on success.

        Args:
            text: Text to speak.
            voice: Optional Azure ShortName (e.g. 'en-US-JennyNeural') for a
                per-utterance voice override. When None, falls back to the
                configured fast/HD voice in CONFIG.
        """
        if not self._audio_detected:
            _refresh_audio_detection()
            self._audio_detected = True
        if not text or not text.strip():
            return False

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

        with self._speak_lock:
            self._set_state("speaking")
            _chronicle_append("spoken", text, voice=voice, source="direct")
            token = object()
            self._speak_token = token
            self._speak_thread = threading.Thread(
                target=self._speak_worker,
                args=(text.strip(), voice),
                kwargs={"owner_token": token},
                daemon=True,
            )
            self._speak_thread.start()
            return True

    def _tts_level_cb(self, level):
        """Emit AudioLevel from TTS audio stream for badge VU effect."""
        GLib.idle_add(self._emit_audio_level, level)

    def _run_subtitle_progress(self, text, estimated_duration, stop_event):
        """Emit SubtitleUpdate signals every 200ms during TTS playback.

        Runs in a daemon thread alongside TTS. Respects pause and cancel.
        Args:
            text: Full text being spoken.
            estimated_duration: Estimated speech duration in seconds.
            stop_event: threading.Event — set when TTS finishes.
        """
        start_time = time.monotonic()
        pause_accumulated = 0.0
        pause_start = None
        while not stop_event.is_set():
            if state._cancel_event.is_set():
                break
            # Handle pause
            if state._pause_event.is_set():
                if pause_start is None:
                    pause_start = time.monotonic()
                stop_event.wait(timeout=0.2)
                continue
            elif pause_start is not None:
                pause_accumulated += time.monotonic() - pause_start
                pause_start = None

            elapsed = time.monotonic() - start_time - pause_accumulated
            pct = min(99, int((elapsed / estimated_duration) * 100)) if estimated_duration > 0 else 0
            GLib.idle_add(self._emit_subtitle_update, text, estimated_duration, pct)
            stop_event.wait(timeout=0.2)

        # Final emission at 100%
        if not state._cancel_event.is_set():
            GLib.idle_add(self._emit_subtitle_update, text, estimated_duration, 100)

    def _subtitle_queue_worker(self, subtitle_q):
        """Single subtitle thread that processes (text, duration) tuples from a queue.

        Eliminates per-sentence thread creation overhead (~5-10ms each).
        Reads from subtitle_q until a None sentinel is received.
        """
        while True:
            item = subtitle_q.get()
            if item is None:
                break
            text, estimated_duration, stop_event = item
            self._run_subtitle_progress(text, estimated_duration, stop_event)

    def _speak_worker(self, text, voice=None, quality=None, output_file=None,
                      user_initiated=True, suppress_idle=False,
                      owner_token=None):
        """TTS using speech_tts.tts().

        Runs as a background thread for user speech (speak()) and
        synchronously inside the queue dispatcher for HTTP items.
        Returns "done" or "error" (the dispatcher records per-item outcomes).
        """
        outcome = "done"
        try:
            state._cancel_event.clear()
            q = quality or self._voice_quality
            # Update HTTP progress tracking
            with self._http_progress_lock:
                self._http_progress["text"] = text[:80]
                self._http_progress["started_at"] = time.time()
                self._http_progress["estimated_duration"] = max(1.0, len(text) / 22.0)
                self._http_progress["pause_accumulated"] = 0.0
                self._http_progress["pause_started"] = 0.0
            # Show text being spoken as live subtitle on badge
            GLib.idle_add(self._emit_partial_transcription, text)

            # Start subtitle progress thread for progressive reveal
            speed_factor = 22.0 if q == "fast" else 15.0
            est_dur = max(1.0, len(text) / speed_factor)
            sub_stop = threading.Event()
            sub_thread = threading.Thread(
                target=self._run_subtitle_progress,
                args=(text, est_dur, sub_stop),
                daemon=True,
            )
            sub_thread.start()

            try:
                result = speech_tts.tts(text, quality=q, progress_token=None,
                                        voice=voice,
                                        speed=CONFIG.get("speed", 1.0),
                                        pitch=CONFIG.get("pitch", "default"),
                                        volume=CONFIG.get("volume", "default"),
                                        audio_level_cb=self._tts_level_cb,
                                        output_file=output_file)
                if result.get("error"):
                    GLib.idle_add(self._emit_error, result["error"])
                    outcome = "error"
            finally:
                # Always stop subtitle thread, even if TTS raises
                sub_stop.set()
                sub_thread.join(timeout=1.0)
        except Exception as exc:
            log.exception("Speak failed: %s", exc)
            GLib.idle_add(self._emit_error, f"Speak failed: {exc}")
            outcome = "error"
        finally:
            # Clear HTTP progress to idle state
            with self._http_progress_lock:
                self._http_progress = {
                    "text": "", "elapsed": 0.0, "estimated_duration": 0.0,
                    "percent": 0, "started_at": 0.0,
                    "pause_accumulated": 0.0, "pause_started": 0.0,
                }
            # Only transition to idle if we're still in speaking state.
            # Read state under lock, then call _set_state outside (it acquires its own lock).
            with self._state_lock:
                still_speaking = self._state == "speaking"
            if (still_speaking and not suppress_idle
                    and self._speak_token is owner_token):
                self._set_state("idle")
            _schedule_warmup()
            if user_initiated:
                self._user_speech_active.clear()

            # Hands-free loop: after TTS finishes in conversation mode,
            # automatically restart listening if continuous_dictation is enabled.
            # user_initiated guard: queued agent chatter must not re-open the mic.
            if (user_initiated
                    and CONFIG.get("continuous_dictation", False)
                    and CONFIG.get("conversation_mode", False)
                    and not self._stop_event.is_set()):
                log.info("Hands-free: auto-restarting listening after TTS")
                GLib.idle_add(lambda: (self.start_listening(quick=True), False)[-1]
                              if (CONFIG.get("continuous_dictation", False)
                                  and CONFIG.get("conversation_mode", False)
                                  and not self._stop_event.is_set())
                              else False)
        return outcome

    def speak_clipboard(self):
        """Read clipboard and speak its contents."""
        text = clipboard_read()
        if not text or not text.strip():
            GLib.idle_add(self._emit_error, "Clipboard is empty")
            return False
        return self.speak(text)

    def speak_selection(self):
        """Read the currently selected/highlighted text and speak it."""
        text = selection_read()
        if not text or not text.strip():
            GLib.idle_add(self._emit_error, "No text selected")
            return False
        return self.speak(text)

    # -- PlaySound (non-blocking chime playback) ----------------------------

    def play_sound(self, sound_name):
        """Play a realm sound chime by name via ~/.realmwatch/sounds/play.sh.

        Supported names: quest_complete, level_up, xp_gain, threat_alert
        Non-blocking — fires and forgets.
        """
        import subprocess
        import os
        script = os.path.expanduser("~/.realmwatch/sounds/play.sh")
        if not os.path.isfile(script):
            log.warning("Sound script not found: %s", script)
            return False
        try:
            subprocess.Popen([script, sound_name],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except Exception as e:
            log.exception("PlaySound failed for %s: %s", sound_name, e)
            return False

    # -- Voice spellbook -----------------------------------------------------

    def skip_current(self, item_id=None):
        """Cancel the current queued utterance only; the next one plays.

        item_id: when given, skip ONLY if that id is the item actually
            playing, else no-op returning None. Closes the positional race:
            agent A posts /skip meaning "kill mine", but by arrival A's item
            has finished and B's is playing — unscoped, A silently kills B.

        The id check and the cancel happen under _queue_current_lock together.
        The dispatcher takes that same lock to claim and to clear the current
        item, so holding it here means the item we verified cannot be swapped
        out from under the cancel. cancel_active() only sets an event and
        signals tracked subprocesses — it never takes this lock, so there is
        no deadlock path.
        """
        with self._queue_current_lock:
            current = self._queue_current
            if current is None:
                return None
            if item_id is not None and current.id != item_id:
                return None
            state.cancel_active()
            return current.id

    def _spell_speak(self, text, voice=None):
        """Spell replies ride the speech queue — same ordering/preemption
        contract as agent speech; a chatty spell can't stomp dictation."""
        if not text:
            return
        try:
            self.enqueue_speech(text, voice=voice)
        except queue.Full:
            log.warning("Spell reply dropped: speech queue full")

    def _spell_ctx_dbus(self, op):
        """Self-directed spell operations (dbus_self action type)."""
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
            new = not CONFIG.get("read_notifications", False)
            self._save_config_flag("read_notifications", new)
            return "The notification herald is %s." % ("on" if new else "off")
        elif op == "wake_word_toggle":
            new = not CONFIG.get("wake_word", False)
            self._save_config_flag("wake_word", new)
            return "The waking watch is %s." % ("on" if new else "off")
        elif op == "thinking_toggle":
            new = not CONFIG.get("llm_thinking", False)
            self._save_config_flag("llm_thinking", new)
            if new:
                return ("Deep thought engaged — replies will be slow "
                        "and thorough.")
            return "Deep thought off — fast replies."
        elif op == "subtitles_toggle":
            new = not CONFIG.get("live_subtitles", True)
            self._save_config_flag("live_subtitles", new)
            # Dual-write: the extension gates its overlay on GSettings —
            # keep both layers in sync (see the config dual-write gotcha).
            schema_dir = os.path.expanduser(
                "~/.local/share/gnome-shell/extensions/"
                "gnome-speaks@jphein/schemas")
            try:
                subprocess.run(
                    ["gsettings", "--schemadir", schema_dir,
                     "set", "org.gnome.shell.extensions.gnome-speaks",
                     "live-subtitles", "true" if new else "false"],
                    capture_output=True, timeout=5)
            except Exception:
                log.warning("Subtitles gsettings sync failed")
            return "Subtitles %s." % ("on" if new else "off")
        elif op == "echo":
            entry = self.respeak(None)
            if entry is None:
                return "The chronicle holds nothing to echo."
            return None  # the respeak itself is the reply
        elif op == "chronicle_recent":
            entries = _chronicle_read(limit=3)
            if not entries:
                return "The chronicle is empty."
            parts = []
            for e in entries:
                who = "You said" if e["kind"] == "you" else "I said"
                text = e["text"]
                if len(text) > 120:
                    text = text[:120] + "…"
                parts.append(f"{who}: {text}")
            return " … ".join(parts)
        elif op == "chronicle_toggle":
            new = not CONFIG.get("chronicle", True)
            self._save_config_flag("chronicle", new)
            if new:
                return "The chronicle records once more."
            return "The chronicle is sealed — nothing will be written."
        else:
            raise ValueError(f"unknown dbus_self op: {op}")
        return None

    def _wake_watcher(self):
        """Daemon thread: streams mic audio to the Wyoming wake-word server
        while idle; a detection acts like the dictation hotkey."""
        last_fail_log = 0.0
        while True:
            if (not CONFIG.get("wake_word", False)
                    or not CONFIG.get("wyoming_host", "")
                    or not CONFIG.get("wake_word_model", "")
                    or self.current_state != "idle"):
                time.sleep(0.5)
                continue
            host = CONFIG.get("wyoming_host", "")
            port = int(CONFIG.get("wyoming_wake_port", 10400))
            model = CONFIG.get("wake_word_model", "")
            proc = None
            try:
                proc = subprocess.Popen(_build_rec_cmd(),
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)

                def _chunks():
                    while (CONFIG.get("wake_word", False)
                           and self.current_state == "idle"):
                        data = proc.stdout.read(3200)  # ~100 ms @ 16 kHz s16
                        if not data:
                            return
                        yield data

                name = wyoming_mod.detect_stream(host, port, model, _chunks())
                if name and self.current_state == "idle":
                    log.info("Wake word detected (%s) — opening mic", name)
                    GLib.idle_add(lambda: (self.start_listening(quick=True),
                                           False)[-1])
                    time.sleep(2.0)  # cooldown; state flips to listening anyway
            except wyoming_mod.WyomingError as e:
                now = time.time()
                if now - last_fail_log > 300:
                    log.warning("Wake watcher: %s (retrying every 60s)", e)
                    last_fail_log = now
                time.sleep(60)
            except Exception:
                log.exception("Wake watcher error")
                time.sleep(10)
            finally:
                if proc is not None:
                    proc.kill()  # pw-record ignores SIGTERM
                    try:
                        proc.wait(timeout=1)
                    except Exception:
                        pass

    def _drain_speech_gap(self, max_seconds=20):
        """Loop-mode gap: hold the mic closed until queued speech plays out.

        Without this, continuous listening keeps state==listening ~100% of
        the time and the queue's never-speak-over-an-open-mic rule starves
        spell replies and agent messages forever. No-op when the queue is
        empty; bails immediately on stop."""
        with self._queue_current_lock:
            busy = self._queue_current is not None
        if not busy and self._tts_queue.empty():
            return
        self._set_state("idle")
        deadline = time.time() + max_seconds
        while time.time() < deadline and not self._stop_event.is_set():
            with self._queue_current_lock:
                busy = self._queue_current is not None
            if not busy and self._tts_queue.empty():
                return
            time.sleep(0.2)

    def _spellbook_stat(self):
        return tuple(os.path.getmtime(p) if os.path.isfile(p) else 0
                     for p in self._spellbook_paths)

    def _maybe_reload_spellbook(self):
        mtimes = self._spellbook_stat()
        if mtimes != self._spellbook_mtimes:
            self._spellbook = spellbook.load_spellbook(*self._spellbook_paths)
            self._spellbook_mtimes = mtimes

    def _try_cast(self, text):
        """Route 'cast …' utterances to the spellbook. True = consumed
        (the text must not be typed or sent to the LLM)."""
        self._maybe_reload_spellbook()
        kind, spell, remainder = spellbook.match(text, self._spellbook)
        if kind == "miss":
            return False
        if kind == "fizzle":
            log.info("CAST | fizzle | nothing matched %r",
                     (remainder or "")[:60])
            self._spell_speak(spellbook.FIZZLE_TEXT)
            return True
        # Execute off-thread: actions may block on network/confirm, and the
        # caller is an STT worker that needs to wrap up its cycle.
        threading.Thread(target=self._spell_executor.cast,
                         args=(spell, remainder), daemon=True,
                         name=f"spell-{spell['name']}").start()
        return True

    # -- Talk (full-duplex TTS+STT) ----------------------------------------

    def talk(self, text):
        """Speak text and listen for user reply via full-duplex TTS+STT.

        Returns the user's spoken reply text, or an error string prefixed
        with 'error:'.
        """
        if not text or not text.strip():
            return "error: no text provided"

        # Talk blocks until the exchange completes, so one try/finally holds
        # the speech queue for the whole call (user outranks agents).
        self._user_speech_active.set()
        try:
            with self._talk_lock:
                if self.current_state not in ("idle",):
                    self.stop(drain_queue=False)

                if not CONFIG.get("key"):
                    GLib.idle_add(self._emit_error, "Azure Speech key not configured")
                    return "error: no API key"

                self._stop_event.clear()
                self._set_state("speaking")
                # Claim playback ownership so a preempted queue worker's
                # cleanup can't reset our speaking state.
                self._speak_token = object()

                # Use an event to pass the result back from the worker thread
                result_holder = {"reply": ""}
                done_event = threading.Event()

                self._talk_thread = threading.Thread(
                    target=self._talk_worker,
                    args=(text.strip(), result_holder, done_event),
                    daemon=True,
                )
                self._talk_thread.start()

                # Wait for the worker to complete (blocks the D-Bus call)
                done_event.wait()
                return result_holder["reply"]
        finally:
            self._user_speech_active.clear()

    def _talk_worker(self, text, result_holder, done_event):
        """Background thread: full-duplex TTS+STT via speech_tts.talk_fullduplex()."""
        try:
            state._cancel_event.clear()
            # Show text being spoken as live subtitle on badge
            GLib.idle_add(self._emit_partial_transcription, text)

            # Start subtitle progress thread for progressive reveal during TTS
            speed_factor = 22.0 if self._voice_quality == "fast" else 15.0
            est_dur = max(1.0, len(text) / speed_factor)
            sub_stop = threading.Event()
            sub_thread = threading.Thread(
                target=self._run_subtitle_progress,
                args=(text, est_dur, sub_stop),
                daemon=True,
            )
            sub_thread.start()

            result = speech_tts.talk_fullduplex(
                text, quality=self._voice_quality,
                audio_level_cb=self._tts_level_cb,
                partial_cb=lambda t: GLib.idle_add(self._emit_partial_transcription, t),
            )

            # Stop subtitle progress thread
            sub_stop.set()
            sub_thread.join(timeout=1.0)

            if result.get("error"):
                GLib.idle_add(self._emit_error, result["error"])
                result_holder["reply"] = f"error: {result['error']}"
            elif result.get("cancelled"):
                result_holder["reply"] = ""
            else:
                user_reply = result.get("text", "")
                result_holder["reply"] = user_reply
                if user_reply:
                    GLib.idle_add(self._emit_transcription_ready, user_reply)
        except Exception as exc:
            log.exception("Talk failed: %s", exc)
            GLib.idle_add(self._emit_error, f"Talk failed: {exc}")
            result_holder["reply"] = f"error: {exc}"
        finally:
            self._set_state("idle")
            _schedule_warmup()
            done_event.set()

    def set_language(self, language):
        """Change the STT language at runtime."""
        CONFIG["language"] = language
        log.info("Language set to: %s", language)
        _invalidate_stt_ws()
        return True

    def get_language(self):
        return CONFIG.get("language", "en-US")

    def toggle_conversation_mode(self):
        current = CONFIG.get("conversation_mode", False)
        self._save_config_flag("conversation_mode", not current)
        if current:
            # Turning off — clear conversation history
            with self._conversation_lock:
                self._conversation_history.clear()
        log.info("Conversation mode: %s", not current)
        # If currently listening, restart so live_typing is recalculated.
        # Signal stop non-blocking, then poll for idle before restarting.
        if self.current_state == "listening":
            log.info("Restarting listen for conversation mode change")
            self._stop_event.set()
            state.cancel_active()
            def _restart_when_idle():
                if self.current_state not in ("idle", "processing"):
                    return True  # keep polling
                if self.current_state == "processing":
                    return True  # still winding down
                self.start_listening()
                return False  # stop polling
            GLib.timeout_add(50, _restart_when_idle)
        return not current

    def toggle_continuous_dictation(self):
        current = CONFIG.get("continuous_dictation", False)
        self._save_config_flag("continuous_dictation", not current)
        log.info("Continuous dictation: %s", not current)
        return not current

    def toggle_barge_in(self):
        current = CONFIG.get("enable_barge_in", False)
        self._save_config_flag("enable_barge_in", not current)
        log.info("Barge-in: %s", not current)
        return not current

    def get_barge_in(self):
        return CONFIG.get("enable_barge_in", False)

    def get_continuous_dictation(self):
        return CONFIG.get("continuous_dictation", False)

    def get_conversation_mode(self):
        return CONFIG.get("conversation_mode", False)

    def get_hands_free(self):
        conv = CONFIG.get("conversation_mode", False)
        cont = CONFIG.get("continuous_dictation", False)
        return conv and cont

    def toggle_terminal_mode(self):
        current = CONFIG.get("terminal_mode", False)
        self._save_config_flag("terminal_mode", not current)
        log.info("Terminal mode: %s", not current)
        return not current

    def get_terminal_mode(self):
        return CONFIG.get("terminal_mode", False)

    def toggle_hands_free(self):
        """Toggle hands-free mode: enables both continuous_dictation + conversation_mode together."""
        # If either is off, turn both on; if both are on, turn both off
        conv = CONFIG.get("conversation_mode", False)
        cont = CONFIG.get("continuous_dictation", False)
        if conv and cont:
            self._save_config_flag("conversation_mode", False)
            self._save_config_flag("continuous_dictation", False)
            with self._conversation_lock:
                self._conversation_history.clear()
            log.info("Hands-free mode: off")
            return False
        else:
            self._save_config_flag("conversation_mode", True)
            self._save_config_flag("continuous_dictation", True)
            log.info("Hands-free mode: on")
            return True

    def toggle_voice_quality(self):
        """Toggle between HD (DragonHD, eastus) and Fast (Neural, westus) voice modes.

        Returns the new quality string: 'fast' or 'hd'.
        """
        current = self._voice_quality
        if current == "hd":
            self._voice_quality = "fast"
            # Use STT region for faster TTS latency
            CONFIG["tts_region"] = None
            CONFIG["tts_key"] = None
            log.info("Voice quality: fast (%s, region=%s)",
                     CONFIG["fast_voice"], CONFIG["region"])
        else:
            self._voice_quality = "hd"
            # Restore HD region for DragonHD voices
            CONFIG["tts_region"] = self._original_tts_region
            CONFIG["tts_key"] = self._original_tts_key
            log.info("Voice quality: hd (%s, region=%s)",
                     CONFIG["voice"], CONFIG.get("tts_region") or CONFIG["region"])
        return self._voice_quality

    def get_voice_quality(self):
        return self._voice_quality

    def get_audio_info(self):
        """Return JSON string with detected audio device and echo cancellation info."""
        import json as _json
        _refresh_audio_detection()
        dev_type = CONFIG.get("_detected_output", "unknown")
        dev_info = CONFIG.get("_detected_output_info", {})
        ec = has_echo_cancel()
        return _json.dumps({
            "device_type": dev_type,
            "echo_cancel": ec,
            "half_duplex": CONFIG.get("half_duplex", False),
            "description": dev_info.get("description", ""),
        })

    def set_stt_mode(self, mode):
        """Set the STT mode. Valid: auto, streaming, whisper, vad, fixed."""
        valid = ("auto", "streaming", "whisper", "vad", "fixed")
        if mode not in valid:
            log.warning("Invalid STT mode: %s (valid: %s)", mode, ", ".join(valid))
            return False
        self._stt_mode = mode
        log.info("STT mode set to: %s", mode)
        return True

    def get_stt_mode(self):
        return self._stt_mode

    def get_stt_modes(self):
        """Return comma-separated list of available STT modes."""
        modes = ["auto"]
        if HAS_WS and HAS_VAD:
            modes.append("streaming")
        if HAS_WHISPER:
            modes.append("whisper")
        if HAS_VAD:
            modes.append("vad")
        modes.append("fixed")
        return ",".join(modes)

    # -- Conversation mode (voice -> LLM -> TTS) --------------------------

    _TYPE_TAG_RE = re.compile(r'<type>(.*?)</type>', re.DOTALL)

    _INTENT_PATTERNS = {
        'time_query': re.compile(
            r'\b(what time|what\'s the time|current time|what day'
            r'|what is today|what date|when is it|today\'s date)\b', re.I),
        'clipboard': re.compile(
            r'\b(clipboard|paste|pasted|copied|what I copied)\b', re.I),
        'app_context': re.compile(
            r'\b(this window|this app|what app|what window|focused'
            r'|current app|screen|what am I (using|running|in))\b', re.I),
    }

    _BASE_SYSTEM_PROMPT = (
        "You are a voice assistant. Be terse \u2014 short sentences, no filler, no preamble. "
        "Answer directly.\n"
        "When asked to type, write, or draft text, wrap it in <type>...</type> tags. "
        "Everything else is spoken aloud.\n"
        "Only use <type> tags when the user explicitly asks you to type or write something."
    )

    # Parses gdbus Eval output like "(true, 'some-app')" → 'some-app'
    _GDBUS_EVAL_RE = re.compile(r"\(true,\s*'([^']*)'\)")

    def _detect_intents(self, text):
        """Return list of intent keys matched by keyword patterns."""
        return [k for k, pat in self._INTENT_PATTERNS.items() if pat.search(text)]

    def _get_focused_app(self):
        """Focused window WM_CLASS (+ title) via the extension's
        org.gnome.Speaks.Desktop interface. GNOME locked down Shell.Eval
        (gnome-speaks#7), so the extension answers from inside the Shell.
        Returns None when the extension isn't loaded — headless still works."""
        try:
            result = subprocess.run(
                ["gdbus", "call", "--session",
                 "--dest", "org.gnome.Speaks.Desktop",
                 "--object-path", "/org/gnome/Speaks/Desktop",
                 "--method", "org.gnome.Speaks.Desktop.GetFocusedApp"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                parts = re.findall(r"'((?:[^'\\]|\\.)*)'", result.stdout)
                wm_class = parts[0] if parts else ""
                title = parts[1] if len(parts) > 1 else ""
                if wm_class and title:
                    return f"{wm_class} — {title}"
                return wm_class or None
        except Exception as exc:
            log.debug("Failed to get focused app: %s", exc)
        return None

    def _get_clipboard_text(self):
        """Get clipboard text (Wayland first, X11 fallback), truncated to 200 chars."""
        for cmd in [["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"]]:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=1)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()[:200]
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        return None

    def _build_context(self, user_text):
        """Build system prompt with dynamic context. Returns (system_prompt, history)."""
        from datetime import datetime
        from concurrent.futures import ThreadPoolExecutor, as_completed

        custom = CONFIG.get("llm_system_prompt", "")
        parts = [custom] if custom else [self._BASE_SYSTEM_PROMPT]

        # Terminal mode: inject command-generation context
        if CONFIG.get("terminal_mode", False):
            parts.append(
                "TERMINAL MODE IS ON. The user is working in a terminal. "
                "When they describe something to run, wrap the exact command in <type>...</type> tags. "
                "Keep explanations extremely brief — prefer just the command. "
                "Use lowercase, no markdown, no code fences. "
                "If they ask a general question, answer normally (spoken aloud)."
            )

        # Intent-based dynamic context injection
        intents = self._detect_intents(user_text)

        if 'time_query' in intents:
            parts.append(f"Current time: {datetime.now().strftime('%I:%M %p, %A %B %d, %Y')}")

        # Run subprocess-based context fetches in parallel
        futures = {}
        need_app = 'app_context' in intents
        need_clip = 'clipboard' in intents
        if need_app or need_clip:
            with ThreadPoolExecutor(max_workers=2) as pool:
                if need_app:
                    futures['app'] = pool.submit(self._get_focused_app)
                if need_clip:
                    futures['clip'] = pool.submit(self._get_clipboard_text)
            if 'app' in futures:
                app = futures['app'].result()
                if app:
                    parts.append(f"Focused application: {app}")
            if 'clip' in futures:
                clip = futures['clip'].result()
                if clip:
                    parts.append(f"Clipboard content: {clip}")

        system_prompt = "\n".join(parts)

        # Trim history to last 20 exchanges (40 messages)
        with self._conversation_lock:
            history = list(self._conversation_history[-40:])
        return system_prompt, history

    def _maybe_loop_restart(self):
        """Restart listening in AI+Loop mode. Called from worker thread."""
        if (CONFIG.get("continuous_dictation", False)
                and CONFIG.get("conversation_mode", False)
                and not self._stop_event.is_set()):
            log.info("AI+Loop: quick-restart listening")
            _schedule_warmup()
            # Direct call from worker thread — skip config reload and
            # audio detection since nothing changed within the loop.
            # Use GLib.idle_add because start_listening touches state
            # that must be set from the main thread context.
            GLib.idle_add(lambda: (self.start_listening(quick=True), False)[-1]
                          if not self._stop_event.is_set()
                          else False)
        else:
            _schedule_warmup()

    def _parse_type_tags(self, reply):
        """Extract <type>...</type> content and remaining spoken text.

        Returns (type_text, speak_text). type_text is the concatenated
        content of all <type> tags (to be typed at cursor). speak_text
        is everything else (to be spoken aloud).
        """
        type_parts = self._TYPE_TAG_RE.findall(reply)
        type_text = "\n".join(type_parts) if type_parts else ""
        speak_text = self._TYPE_TAG_RE.sub("", reply).strip()
        # Clean up leftover whitespace from tag removal
        speak_text = re.sub(r'\s{2,}', ' ', speak_text)
        return type_text, speak_text

    # -- Sentence splitting for streaming TTS ------------------------------

    _SENTENCE_BOUNDARY_RE = re.compile(
        r'(?<=[.!?])'   # lookbehind for sentence-ending punctuation
        r'(?:\s+|$)'    # followed by whitespace or end-of-string
    )

    def _split_sentences(self, buffer):
        """Split buffer into (complete_sentences_list, remaining_buffer).

        A sentence is considered complete when it ends with . ! or ?
        followed by whitespace (or end of string, but only if the stream
        has finished — callers should only pass is_final=True at the end).
        Returns (list_of_sentences, leftover_buffer).
        """
        parts = self._SENTENCE_BOUNDARY_RE.split(buffer)
        # Filter out empty strings from split
        parts = [p for p in parts if p.strip()]
        if len(parts) <= 1:
            return [], buffer  # no complete sentence yet
        # All but the last part are complete sentences
        complete = parts[:-1]
        remaining = parts[-1]
        return complete, remaining

    # -- Streaming LLM response with incremental TTS ----------------------

    def _stream_conversation_worker(self, user_text):
        """Stream LLM response and start TTS on each complete sentence.

        Uses the unified llm_stream library for all providers (including bedrock).
        """
        # AI replies are user-initiated speech: hold the agent speech queue
        # for the whole turn (LLM streaming + sentence TTS).
        self._user_speech_active.set()
        try:
            provider = CONFIG.get("llm_provider", "anthropic")
            model = CONFIG.get("llm_model", "claude-opus-4.6")

            system_prompt, history = self._build_context(user_text)

            # Build messages and config for stream_chat
            messages = list(history) + [{"role": "user", "content": user_text}]
            cfg = {"api_key": CONFIG.get("llm_api_key", "")}
            cfg.update(self._load_cca_config())
            if provider == "local":
                # llm_thinking (config + "cast deep thought") decides whether
                # a reasoning model may think before answering — off = fast
                # voice replies, on = slower but deeper. Overrides only the
                # enable_thinking key; other local_extra_body fields survive.
                extra = dict(cfg.get("local_extra_body") or {})
                ctk = dict(extra.get("chat_template_kwargs") or {})
                ctk["enable_thinking"] = bool(CONFIG.get("llm_thinking", False))
                extra["chat_template_kwargs"] = ctk
                cfg["local_extra_body"] = extra

            try:
                token_iter = stream_chat(
                    provider=provider,
                    model=model,
                    messages=messages,
                    system_prompt=system_prompt,
                    config=cfg,
                )
            except LLMStreamError as exc:
                GLib.idle_add(self._emit_error, str(exc))
                self._set_state("idle")
                return

            # --- Consume token stream, buffer sentences, speak incrementally ---

            full_reply = []     # all tokens for conversation history
            buffer = ""         # accumulates tokens until sentence boundary
            in_type_tag = False # True while inside <type>...</type>
            first_sentence = True
            spoke_anything = False

            # Single subtitle thread for the entire conversation (Fix 7)
            subtitle_q = queue.Queue()
            subtitle_thread = threading.Thread(
                target=self._subtitle_queue_worker,
                args=(subtitle_q,), daemon=True)
            subtitle_thread.start()

            for token in token_iter:
                if self._stop_event.is_set():
                    log.info("Streaming aborted — stop event set")
                    break
                full_reply.append(token)

                # Track <type> tag state so we don't speak tagged content.
                # Accumulate tagged content silently; it gets pasted at the end.
                pending = token
                while pending:
                    if in_type_tag:
                        close_idx = pending.find("</type>")
                        if close_idx >= 0:
                            # End of type tag — skip content, resume after
                            in_type_tag = False
                            pending = pending[close_idx + 7:]
                        else:
                            # Still inside type tag — consume entirely
                            pending = ""
                    else:
                        open_idx = pending.find("<type>")
                        if open_idx >= 0:
                            # Text before the tag is speakable
                            buffer += pending[:open_idx]
                            in_type_tag = True
                            pending = pending[open_idx + 6:]
                        else:
                            # Check for partial "<type" at end of pending
                            # to avoid speaking an incomplete tag opener
                            partial = ""
                            for i in range(1, min(6, len(pending) + 1)):
                                if "<type>"[:i] == pending[-i:]:
                                    partial = pending[-i:]
                                    pending = pending[:-i]
                                    break
                            buffer += pending
                            # Put partial back — next token will complete it
                            # or it will be flushed as text
                            buffer += partial
                            pending = ""

                # Check for complete sentences in the buffer
                sentences, buffer = self._split_sentences(buffer)
                for sentence in sentences:
                    sentence = sentence.strip()
                    if not sentence:
                        continue

                    if first_sentence:
                        # Transition to speaking state on first sentence
                        self._set_state("speaking")
                        self._speak_token = object()  # claim playback ownership
                        state._cancel_event.clear()
                        # On headphones, prewarm recorder during TTS
                        if not CONFIG.get("half_duplex", False):
                            _schedule_warmup()
                        first_sentence = False

                    spoke_anything = True
                    log.info("Streaming TTS sentence: %s", sentence[:80])
                    GLib.idle_add(self._emit_partial_transcription, sentence)
                    _sf = 22.0 if self._voice_quality == "fast" else 15.0
                    _sd = max(1.0, len(sentence) / _sf)
                    _ss = threading.Event()
                    subtitle_q.put((sentence, _sd, _ss))
                    speech_tts.tts(sentence, quality=self._voice_quality,
                                   speed=CONFIG.get("speed", 1.0),
                                   pitch=CONFIG.get("pitch", "default"),
                                   volume=CONFIG.get("volume", "default"),
                                   audio_level_cb=self._tts_level_cb)
                    _ss.set()

                    if self._stop_event.is_set():
                        break

            # Speak any remaining buffered text after stream ends
            remainder = buffer.strip()
            if remainder and not self._stop_event.is_set():
                if first_sentence:
                    self._set_state("speaking")
                    self._speak_token = object()  # claim playback ownership
                    state._cancel_event.clear()
                    if not CONFIG.get("half_duplex", False):
                        _schedule_warmup()
                    first_sentence = False
                spoke_anything = True
                log.info("Streaming TTS remainder: %s", remainder[:80])
                GLib.idle_add(self._emit_partial_transcription, remainder)
                _sf = 22.0 if self._voice_quality == "fast" else 15.0
                _sd = max(1.0, len(remainder) / _sf)
                _ss = threading.Event()
                subtitle_q.put((remainder, _sd, _ss))
                speech_tts.tts(remainder, quality=self._voice_quality,
                               speed=CONFIG.get("speed", 1.0),
                               pitch=CONFIG.get("pitch", "default"),
                               volume=CONFIG.get("volume", "default"),
                               audio_level_cb=self._tts_level_cb)
                _ss.set()

            # Stop subtitle queue worker and wait for it to finish
            subtitle_q.put(None)
            subtitle_thread.join(timeout=2.0)

            # --- Post-stream: history, type tags, state transitions ---

            reply = "".join(full_reply)
            if reply:
                # Whole reply as one chronicle entry — the sentence-level TTS
                # calls above would fragment it into respeak-useless shards.
                _chronicle_append("spoken", reply, source="assistant")
                with self._conversation_lock:
                    self._conversation_history.append({"role": "user", "content": user_text})
                    self._conversation_history.append({"role": "assistant", "content": reply})
                    if len(self._conversation_history) > 100:
                        self._conversation_history = self._conversation_history[-100:]

                # Handle <type> tags from accumulated reply (terminal mode)
                type_text, _speak_text = self._parse_type_tags(reply)
                if type_text:
                    log.info("Typing %d chars at cursor (from streamed reply)", len(type_text))
                    _clipboard_paste(type_text)

            # Half-duplex drain
            if spoke_anything and CONFIG.get("half_duplex", False):
                time.sleep(0.5)

            self._set_state("idle")
            self._maybe_loop_restart()

        except Exception as exc:
            log.exception("Streaming conversation failed: %s", exc)
            GLib.idle_add(self._emit_error, f"LLM error: {exc}")
            self._set_state("idle")
            # Still try to restart the loop after a brief delay so a transient
            # error (network blip, rate limit) doesn't kill the conversation.
            if (CONFIG.get("continuous_dictation", False)
                    and CONFIG.get("conversation_mode", False)
                    and not self._stop_event.is_set()):
                log.info("AI+Loop: retry after error (2s delay)")
                GLib.timeout_add(2000, lambda: (
                    (self.start_listening(quick=True), False)[-1]
                    if not self._stop_event.is_set()
                    else False
                ))
        finally:
            self._user_speech_active.clear()

    # -- Conversation worker ------------------------------------------------

    def _conversation_worker(self, user_text):
        """Send transcribed text to an LLM and speak the response.

        All providers (including bedrock) now stream via llm_stream.
        """
        if not stream_chat:
            GLib.idle_add(self._emit_error, "LLM streaming library not available (llm_stream not found)")
            self._set_state("idle")
            return
        return self._stream_conversation_worker(user_text)

    def _load_cca_config(self):
        """Load cloud-chat-assistant config."""
        path = os.path.expanduser("~/.config/cloud-chat-assistant/config.json")
        try:
            with open(path) as f:
                import json as _json
                return _json.load(f)
        except Exception as exc:
            log.debug("CCA config load failed: %s", exc)
            return {}

    # -- Stop --------------------------------------------------------------

    def stop(self, drain_queue=True):
        """Stop any current operation and return to idle.

        drain_queue: also flush pending HTTP speech-queue items (panic stop —
        D-Bus Stop and POST /stop). User preemption passes False so the agent
        backlog survives and resumes afterwards.
        """
        log.info("Stop requested (current state: %s)", self.current_state)
        if drain_queue:
            self._drain_tts_queue()

        # Signal our stop event for the STT sender loop
        self._stop_event.set()

        # Kill all active procs and signal cancellation
        state.cancel_active()

        # Wait for threads to finish with short timeouts
        # Skip joining the current thread (e.g. conversation mode calls speak() from STT thread)
        me = threading.current_thread()

        with self._stt_lock:
            stt_t = self._stt_thread
        if stt_t is not None and stt_t is not me:
            stt_t.join(timeout=3)
            if stt_t.is_alive():
                log.warning("STT thread did not finish in 3s")
        with self._stt_lock:
            self._stt_thread = None

        with self._speak_lock:
            speak_t = self._speak_thread
        if speak_t is not None and speak_t is not me:
            speak_t.join(timeout=3)
            if speak_t.is_alive():
                log.warning("Speak thread did not finish in 3s")
        with self._speak_lock:
            self._speak_thread = None

        with self._talk_lock:
            talk_t = self._talk_thread
        if talk_t is not None and talk_t is not me:
            talk_t.join(timeout=3)
            if talk_t.is_alive():
                log.warning("Talk thread did not finish in 3s")
        with self._talk_lock:
            self._talk_thread = None

        self._set_state("idle")
        return True

    # -- Cleanup -----------------------------------------------------------

    def shutdown(self):
        """Clean up resources on exit."""
        log.info("Shutting down")
        self.stop()
        _reset_ydotoold()
        _discard_prewarmed_rec()
        _invalidate_stt_ws()


# ---------------------------------------------------------------------------
# HTTP REST API for browser-based TTS control
# ---------------------------------------------------------------------------

class SpeechHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Lightweight REST handler exposing TTS control to localhost callers."""

    service = None  # set to GnomeSpeaksService instance before server starts
    timeout = 10  # seconds — prevents slow/hung clients from blocking worker threads

    # Voices cache: (data, timestamp)
    _voices_cache = (None, 0.0)
    _VOICES_CACHE_TTL = 300  # 5 minutes

    def log_message(self, format, *args):
        """Route HTTP log messages through the existing logger instead of stderr."""
        log.debug("HTTP %s", format % args)

    # -- CORS helpers ------------------------------------------------------

    def _set_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, message):
        self._send_json({"ok": False, "error": message}, status=status)

    # -- Routing -----------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/version":
            self._handle_version()
        elif path == "/status":
            self._handle_status()
        elif path == "/voices":
            self._handle_voices()
        elif path == "/queue":
            self._handle_queue()
        elif path == "/chronicle":
            self._handle_chronicle()
        else:
            self._send_error_json(404, f"Unknown endpoint: {path}")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/speak":
            self._handle_speak()
        elif path == "/stop":
            self._handle_stop()
        elif path == "/pause":
            self._handle_pause()
        elif path == "/resume":
            self._handle_resume()
        elif path == "/skip":
            self._handle_skip()
        elif path == "/cast":
            self._handle_cast()
        elif path == "/respeak":
            self._handle_respeak()
        else:
            self._send_error_json(404, f"Unknown endpoint: {path}")

    # -- Request body parsing ----------------------------------------------

    def _read_json_body(self):
        """Read and parse JSON request body. Returns dict or None on error."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        try:
            raw = self.rfile.read(content_length)
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_error_json(400, f"Invalid JSON: {exc}")
            return None

    # -- Endpoint handlers -------------------------------------------------

    def _handle_speak(self):
        body = self._read_json_body()
        if body is None:
            return  # error already sent
        text = body.get("text", "")
        if not text or not text.strip():
            self._send_error_json(400, "Missing or empty 'text' field")
            return

        # Reject up front rather than enqueueing items doomed to fail —
        # a keyless service must not answer 200 and then say nothing.
        if not CONFIG.get("key"):
            self._send_error_json(503, "Azure Speech key not configured")
            return

        svc = self.service
        # Per-item overrides travel inside the TTSQueueItem — no service-global
        # swaps, so overlapping requests can't race. Voice is an Azure ShortName
        # (e.g. en-US-JennyNeural), sanitized downstream in _prepare_tts.
        quality = body.get("quality") if body.get("quality") in ("fast", "hd") else None
        voice = body.get("voice") or None
        output_file = body.get("output_file") or None

        # Per-source coalescing: "only my latest status matters". With N agents
        # narrating, a deep FIFO guarantees you hear STALE speech; dropping a
        # source's own unspoken backlog fixes that at the root instead of
        # choosing an overflow victim.
        source = body.get("source") or None
        if source is not None:
            source = str(source)[:64]  # bound the key; it lands in logs
        kind = body.get("kind") or None
        # kind:"progress" is coalescing plus SSIP's end-of-burst guarantee
        # ("Completed 100%" must always be heard). In a strict FIFO that
        # guarantee is free: dropping older same-source items always leaves
        # the newest, and the newest is always spoken. So it is the same
        # machinery — documented equivalence, no separate class to maintain.
        coalesce = bool(body.get("coalesce")) or kind == "progress"
        if coalesce and not source:
            self._send_error_json(
                400, "'coalesce'/'kind' requires a 'source' key")
            return

        # interrupt: true — flush everything and speak now (panic + speak).
        # flushed reports the blast radius: this deletes OTHER callers'
        # queued speech (Android hides this ability entirely; we expose it
        # but make it leave a trace).
        flushed = None
        if body.get("interrupt"):
            flushed = svc._drain_tts_queue()
            svc.stop(drain_queue=False)
            # The dispatcher clears _queue_current moments after the cancel;
            # wait briefly so position/state in the response reflect the flush.
            deadline = time.time() + 1.0
            while time.time() < deadline:
                with svc._queue_current_lock:
                    if svc._queue_current is None:
                        break
                time.sleep(0.02)

        try:
            item_id, position, dropped = svc.enqueue_speech(
                text, voice=voice, quality=quality, output_file=output_file,
                source=source, coalesce=coalesce)
        except queue.Full:
            self._send_error_json(429, "queue full")
            return

        state_str = ("speaking" if position == 0
                     and svc.current_state == "idle" else "queued")
        resp = {"ok": True, "id": item_id,
                "position": position, "state": state_str}
        if flushed is not None:
            resp["flushed"] = flushed
        if coalesce:
            # Blast radius, same spirit as interrupt's `flushed` — but scoped
            # to the caller's own source, so it can never surprise a peer.
            resp["coalesced"] = dropped
        self._send_json(resp)

    def _handle_stop(self):
        svc = self.service
        cleared = svc._drain_tts_queue()
        svc.stop(drain_queue=False)
        self._send_json({"ok": True, "state": "idle", "cleared": cleared})

    def _handle_skip(self):
        """Cancel the current queued utterance only; the next one plays.

        Optional {"id": N} scopes the skip to that item — a no-op if it is not
        the one playing. Bodyless /skip keeps the old positional behaviour
        ("move on" is inherently positional when a human says it).
        """
        body = self._read_json_body()
        if body is None:
            return  # error already sent
        item_id = body.get("id")
        if item_id is not None:
            try:
                item_id = int(item_id)
            except (TypeError, ValueError):
                self._send_error_json(400, "'id' must be an integer")
                return
        self._send_json({"ok": True,
                         "skipped": self.service.skip_current(item_id)})

    def _handle_cast(self):
        """Text seam for the spellbook: same path and gates as spoken casts.
        Lets agents cast spells and makes every spell curl-testable."""
        body = self._read_json_body()
        if body is None:
            return
        text = body.get("text", "")
        if not text or not text.strip():
            self._send_error_json(400, "Missing or empty 'text' field")
            return
        handled = self.service._try_cast(text)
        self._send_json({"ok": True, "handled": handled})

    def _handle_queue(self):
        svc = self.service
        with svc._queue_current_lock:
            cur = svc._queue_current
        current = None
        if cur is not None:
            current = {"id": cur.id, "text": cur.text[:80], "voice": cur.voice,
                       "source": cur.source, "enqueued_at": cur.enqueued_at}
        with svc._tts_queue.mutex:
            items = list(svc._tts_queue.queue)
        pending = [{"id": i.id, "text": i.text[:80], "voice": i.voice,
                    "source": i.source, "enqueued_at": i.enqueued_at}
                   for i in items]
        self._send_json({"current": current, "pending": pending,
                         "depth": len(pending),
                         "recent": list(svc._queue_recent)})

    def _handle_chronicle(self):
        """GET /chronicle?limit=20&q=text&kind=you|spoken — newest last."""
        from urllib.parse import urlparse, parse_qs
        params = parse_qs(urlparse(self.path).query)
        try:
            limit = int(params.get("limit", ["20"])[0])
        except ValueError:
            limit = 20
        kind = params.get("kind", [None])[0]
        if kind not in (None, "you", "spoken"):
            self._send_error_json(400, "kind must be 'you' or 'spoken'")
            return
        entries = _chronicle_read(limit=limit, q=params.get("q", [None])[0],
                                  kind=kind)
        self._send_json({"entries": entries, "count": len(entries),
                         "enabled": CONFIG.get("chronicle", True)})

    def _handle_respeak(self):
        """POST /respeak {"id": N} — omit id to replay the last spoken line."""
        body = self._read_json_body()
        if body is None:
            return  # error already sent
        entry = self.service.respeak(body.get("id") or None)
        if entry is None:
            self._send_error_json(404, "Nothing to respeak (bad id, empty "
                                        "chronicle, or queue full)")
            return
        self._send_json({"ok": True, "respeaking": {
            "id": entry["id"], "kind": entry["kind"],
            "text": entry["text"][:80]}})

    def _handle_pause(self):
        svc = self.service
        state.pause_active()
        # Track pause start time for elapsed calculation
        with svc._http_progress_lock:
            if svc._http_progress["started_at"] > 0:
                svc._http_progress["pause_started"] = time.time()
        self._send_json({"ok": True, "paused": True})

    def _handle_resume(self):
        svc = self.service
        state.resume_active()
        # Accumulate pause duration
        with svc._http_progress_lock:
            ps = svc._http_progress["pause_started"]
            if ps > 0:
                svc._http_progress["pause_accumulated"] += time.time() - ps
                svc._http_progress["pause_started"] = 0.0
        self._send_json({"ok": True, "paused": False})

    def _handle_version(self):
        """GET /api/version — realm-sigil version contract (falls back to a
        minimal payload when realm-sigil isn't installed)."""
        import socket as _socket
        repo_dir = os.path.dirname(os.path.abspath(__file__))

        def _git(*args):
            try:
                return subprocess.run(
                    ["git", "-C", repo_dir] + list(args),
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
            except Exception:
                return ""

        hash_ = _git("rev-parse", "--short", "HEAD") or "dev"
        branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
        dirty = bool(_git("status", "--porcelain"))
        uptime = int(time.time() - _SERVICE_START_TIME)
        try:
            sys.path.insert(0, os.path.expanduser("~/Projects/realm-sigil/python"))
            try:
                from realm_sigil import version_dict
            finally:
                sys.path.pop(0)
            payload = version_dict(
                "gnome-speaks", "GNOME Shell voice extension service",
                "fantasy", "https://github.com/techempower-org/gnome-speaks",
                hash=hash_, branch=branch, dirty=dirty,
                built=_SERVICE_START_ISO, started=_SERVICE_START_ISO,
                uptime=uptime,
                runtime="python%d.%d" % sys.version_info[:2],
                host=_socket.gethostname(), pid=os.getpid(),
            )
        except ImportError:
            payload = {"name": "gnome-speaks", "version": hash_,
                       "hash": hash_, "branch": branch, "dirty": dirty,
                       "uptime": uptime}
        self._send_json(payload)

    def _handle_status(self):
        svc = self.service
        current = svc.current_state
        paused = state._pause_event.is_set() if hasattr(state, '_pause_event') else False

        progress = None
        with svc._http_progress_lock:
            p = svc._http_progress
            if p["started_at"] > 0 and current == "speaking":
                pause_acc = p["pause_accumulated"]
                # If currently paused, include ongoing pause time
                if p["pause_started"] > 0:
                    pause_acc += time.time() - p["pause_started"]
                elapsed = time.time() - p["started_at"] - pause_acc
                elapsed = max(0.0, elapsed)
                est = p["estimated_duration"]
                pct = min(100, int((elapsed / est) * 100)) if est > 0 else 0
                progress = {
                    "elapsed": round(elapsed, 1),
                    "estimated_duration": round(est, 1),
                    "percent": pct,
                    "text": p["text"],
                }

        result = {"state": current, "paused": paused,
                  "queue_depth": svc._tts_queue.qsize()}
        if progress is not None:
            result["progress"] = progress
        self._send_json(result)

    def _handle_voices(self):
        now = time.time()
        data, ts = SpeechHTTPHandler._voices_cache
        if data is not None and (now - ts) < self._VOICES_CACHE_TTL:
            self._send_json(data)
            return
        try:
            voices = speech_tts.get_voices()
            SpeechHTTPHandler._voices_cache = (voices, now)
            self._send_json(voices)
        except Exception as exc:
            log.warning("Failed to fetch voices: %s", exc)
            self._send_error_json(500, f"Failed to fetch voices: {exc}")


# ---------------------------------------------------------------------------
# DBus method dispatch
# ---------------------------------------------------------------------------

class DBusHandler:
    """Handles incoming DBus method calls and dispatches to the service."""

    def __init__(self, service):
        self.service = service
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dbus")

    def _run_async(self, invocation, variant_type, fn, *args):
        """Run fn(*args) in the thread pool; return result via GLib.idle_add."""
        def _worker():
            try:
                result = fn(*args)
                GLib.idle_add(
                    lambda: invocation.return_value(
                        GLib.Variant(variant_type, (result,))
                    ) or False
                )
            except Exception as exc:
                log.exception("Async D-Bus call failed: %s", exc)
                GLib.idle_add(
                    lambda: invocation.return_dbus_error(
                        "org.gnome.Speaks.Error", str(exc)
                    ) or False
                )
        self._pool.submit(_worker)

    def handle_method_call(self, connection, sender, object_path, interface_name,
                           method_name, parameters, invocation):
        """GDBus method call handler."""
        try:
            if method_name == "StartListening":
                result = self.service.start_listening()
                invocation.return_value(GLib.Variant("(s)", (result,)))

            elif method_name == "StopListening":
                self._run_async(invocation, "(s)", self.service.stop_listening)

            elif method_name == "Speak":
                text = parameters.unpack()[0]
                self._run_async(invocation, "(b)", self.service.speak, text)

            elif method_name == "SpeakClipboard":
                self._run_async(invocation, "(b)", self.service.speak_clipboard)

            elif method_name == "SpeakSelection":
                self._run_async(invocation, "(b)", self.service.speak_selection)

            elif method_name == "Talk":
                text = parameters.unpack()[0]
                self._run_async(invocation, "(s)", self.service.talk, text)

            elif method_name == "SetLanguage":
                lang = parameters.unpack()[0]
                result = self.service.set_language(lang)
                invocation.return_value(GLib.Variant("(b)", (result,)))

            elif method_name == "GetLanguage":
                result = self.service.get_language()
                invocation.return_value(GLib.Variant("(s)", (result,)))

            elif method_name == "ToggleConversationMode":
                result = self.service.toggle_conversation_mode()
                invocation.return_value(GLib.Variant("(b)", (result,)))

            elif method_name == "ToggleContinuousDictation":
                result = self.service.toggle_continuous_dictation()
                invocation.return_value(GLib.Variant("(b)", (result,)))

            elif method_name == "ToggleVoiceQuality":
                result = self.service.toggle_voice_quality()
                invocation.return_value(GLib.Variant("(s)", (result,)))

            elif method_name == "GetVoiceQuality":
                result = self.service.get_voice_quality()
                invocation.return_value(GLib.Variant("(s)", (result,)))

            elif method_name == "ToggleBargeIn":
                result = self.service.toggle_barge_in()
                invocation.return_value(GLib.Variant("(b)", (result,)))

            elif method_name == "GetBargeIn":
                result = self.service.get_barge_in()
                invocation.return_value(GLib.Variant("(b)", (result,)))

            elif method_name == "ToggleHandsFree":
                result = self.service.toggle_hands_free()
                invocation.return_value(GLib.Variant("(b)", (result,)))

            elif method_name == "GetContinuousDictation":
                result = self.service.get_continuous_dictation()
                invocation.return_value(GLib.Variant("(b)", (result,)))

            elif method_name == "GetConversationMode":
                result = self.service.get_conversation_mode()
                invocation.return_value(GLib.Variant("(b)", (result,)))

            elif method_name == "GetHandsFree":
                result = self.service.get_hands_free()
                invocation.return_value(GLib.Variant("(b)", (result,)))

            elif method_name == "ToggleTerminalMode":
                result = self.service.toggle_terminal_mode()
                invocation.return_value(GLib.Variant("(b)", (result,)))

            elif method_name == "GetTerminalMode":
                result = self.service.get_terminal_mode()
                invocation.return_value(GLib.Variant("(b)", (result,)))

            elif method_name == "GetAudioInfo":
                result = self.service.get_audio_info()
                invocation.return_value(GLib.Variant("(s)", (result,)))

            elif method_name == "SetSTTMode":
                mode = parameters.unpack()[0]
                result = self.service.set_stt_mode(mode)
                invocation.return_value(GLib.Variant("(b)", (result,)))

            elif method_name == "GetSTTMode":
                result = self.service.get_stt_mode()
                invocation.return_value(GLib.Variant("(s)", (result,)))

            elif method_name == "GetSTTModes":
                result = self.service.get_stt_modes()
                invocation.return_value(GLib.Variant("(s)", (result,)))

            elif method_name == "PlaySound":
                sound_name = parameters.unpack()[0]
                result = self.service.play_sound(sound_name)
                invocation.return_value(GLib.Variant("(b)", (result,)))

            elif method_name == "Stop":
                self._run_async(invocation, "(b)", self.service.stop)

            elif method_name == "GetState":
                result = self.service.current_state
                invocation.return_value(GLib.Variant("(s)", (result,)))

            elif method_name == "GetChronicle":
                limit = parameters.unpack()[0]
                entries = _chronicle_read(limit=limit if limit > 0 else 20)
                invocation.return_value(
                    GLib.Variant("(s)", (json.dumps(entries),)))

            elif method_name == "Respeak":
                entry_id = parameters.unpack()[0]
                entry = self.service.respeak(entry_id or None)
                invocation.return_value(
                    GLib.Variant("(b)", (entry is not None,)))

            else:
                invocation.return_dbus_error(
                    "org.gnome.Speaks.UnknownMethod",
                    f"Unknown method: {method_name}",
                )
        except Exception as exc:
            log.exception("Error handling %s", method_name)
            invocation.return_dbus_error(
                "org.gnome.Speaks.InternalError",
                str(exc),
            )


# ---------------------------------------------------------------------------
# Bus ownership callbacks
# ---------------------------------------------------------------------------

SPIEL_BUS_NAME = "org.gnome.Speaks.Speech.Provider"
SPIEL_OBJECT_PATH = "/org/gnome/Speaks/Speech/Provider"
SPIEL_INTERFACE_XML = """
<node>
  <interface name="org.freedesktop.Speech.Provider">
    <method name="Synthesize">
      <arg direction="in" type="h" name="pipe_fd"/>
      <arg direction="in" type="s" name="text"/>
      <arg direction="in" type="s" name="voice_id"/>
      <arg direction="in" type="d" name="pitch"/>
      <arg direction="in" type="d" name="rate"/>
      <arg direction="in" type="b" name="is_ssml"/>
      <arg direction="in" type="s" name="language"/>
    </method>
    <property name="Name" type="s" access="read"/>
    <property name="Voices" type="a(ssstas)" access="read"/>
  </interface>
</node>"""


def _spiel_method_call(connection, sender, object_path, interface_name,
                       method_name, parameters, invocation):
    """Synthesize: pull the pipe fd out of the fd list, ACK the call, and
    write PCM on a per-request thread (the contract expects concurrency)."""
    if method_name != "Synthesize":
        invocation.return_dbus_error(
            "org.freedesktop.DBus.Error.UnknownMethod", "Unknown method")
        return
    handle, text, voice_id, _pitch, _rate, _is_ssml, _lang = \
        parameters.unpack()
    fd_list = invocation.get_message().get_unix_fd_list()
    if fd_list is None or handle >= fd_list.get_length():
        invocation.return_dbus_error(
            "org.freedesktop.DBus.Error.InvalidArgs", "missing pipe fd")
        return
    fd = fd_list.get(handle)  # returns a dup we own
    invocation.return_value(None)
    threading.Thread(target=spiel_provider.synthesize_to_fd,
                     args=(fd, text, voice_id, CONFIG), daemon=True,
                     name="spiel-synth").start()


def _spiel_get_property(connection, sender, object_path, interface_name,
                        property_name):
    if property_name == "Name":
        return GLib.Variant("s", "GNOME Speaks")
    if property_name == "Voices":
        return GLib.Variant("a(ssstas)",
                            spiel_provider.build_voices(CONFIG))
    return None


def _on_spiel_bus_acquired(connection, name):
    node = Gio.DBusNodeInfo.new_for_xml(SPIEL_INTERFACE_XML)
    connection.register_object(
        SPIEL_OBJECT_PATH,
        node.lookup_interface("org.freedesktop.Speech.Provider"),
        _spiel_method_call, _spiel_get_property, None)
    log.info("Spiel provider registered as %s", name)


def on_bus_acquired(connection, name, service, handler):
    """Called when we have a connection to the session bus."""
    log.info("Bus acquired: %s", name)

    node_info = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
    interface_info = node_info.lookup_interface(INTERFACE_NAME)

    connection.register_object(
        OBJECT_PATH,
        interface_info,
        handler.handle_method_call,
        None,  # get_property
        None,  # set_property
    )

    service._connection = connection
    log.info("Object registered at %s", OBJECT_PATH)


def on_name_acquired(connection, name):
    """Called when we successfully own the bus name."""
    log.info("Name acquired: %s", name)


def on_name_lost(connection, name, loop):
    """Called when we lose the bus name (another instance took over, or error)."""
    log.warning("Name lost: %s — exiting", name)
    loop.quit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GNOME Speaks DBus service")
    parser.add_argument(
        "--replace", action="store_true",
        help="Replace an existing instance of the service",
    )
    parser.add_argument(
        "--http-port", type=int,
        default=int(os.environ.get("GNOME_SPEAKS_HTTP_PORT", "7710")),
        help="HTTP REST API port (default: 7710, env: GNOME_SPEAKS_HTTP_PORT)",
    )
    args = parser.parse_args()

    # Validate config
    if not CONFIG.get("key"):
        log.error(
            "No Azure Speech key found. "
            "Set AZURE_SPEECH_KEY or configure ~/.config/speech-to-cli/config.json",
        )
        # Continue anyway — we will emit Error signals on method calls

    log.info(
        "Starting GNOME Speaks service (speech=%s, region=%s, vad=%s, ws=%s, whisper=%s)",
        _SPEECH_ENGINE, CONFIG.get("region"), HAS_VAD, HAS_WS, HAS_WHISPER,
    )

    # Detect typing tool in background to avoid blocking startup with
    # shutil.which() + pidof subprocess calls (~100-200ms).
    def _init_typing():
        _detect_typing_tool()
        _reset_ydotoold()
    threading.Thread(target=_init_typing, daemon=True).start()

    # Detect audio output device and auto-enable echo cancellation
    _refresh_audio_detection()
    dev_type = CONFIG.get("_detected_output", "unknown")
    ec_present = has_echo_cancel()
    if ec_present and dev_type == "headphones":
        CONFIG["enable_echo_cancel"] = True
        log.info("Auto-enabled echo cancellation (headphones + PipeWire EC detected)")
    log.info("Audio output: %s, echo_cancel=%s, half_duplex=%s",
             dev_type, ec_present, CONFIG.get("half_duplex", False))

    # Prewarm recorder, WebSocket, and HTTP session so first call is instant
    _prewarm_recorder()
    # Pre-warm STT WebSocket and TTS HTTP connection in background
    def _prewarm_connections():
        try:
            if HAS_WS:
                _get_stt_ws()
                log.info("STT WebSocket pre-warmed")
        except Exception as exc:
            log.warning("STT WebSocket pre-warm failed (will retry on first use): %s", exc)
        try:
            session = state.get_http_session()
            tts_region = CONFIG.get("tts_region") or CONFIG.get("region")
            if tts_region:
                session.head(f"https://{tts_region}.tts.speech.microsoft.com", timeout=3)
                log.info("TTS HTTP session pre-warmed")
        except Exception as exc:
            log.warning("TTS HTTP pre-warm failed (will retry on first use): %s", exc)
    threading.Thread(target=_prewarm_connections, daemon=True).start()

    # Create service and handler
    service = GnomeSpeaksService()
    service._audio_detected = True  # startup detection already ran above
    handler = DBusHandler(service)

    # Set up main loop
    loop = GLib.MainLoop()
    service._main_loop = loop

    # Request bus name
    flags = Gio.BusNameOwnerFlags.NONE
    if args.replace:
        flags = Gio.BusNameOwnerFlags.REPLACE

    # Spiel speech provider: second bus name, config-gated. The ".Speech.
    # Provider" suffix is how libspiel clients discover providers.
    if CONFIG.get("spiel_provider", False):
        Gio.bus_own_name(
            Gio.BusType.SESSION, SPIEL_BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            _on_spiel_bus_acquired, None,
            lambda conn, name: log.warning("Spiel name lost: %s", name))

    owner_id = Gio.bus_own_name(
        Gio.BusType.SESSION,
        BUS_NAME,
        flags,
        lambda conn, name: on_bus_acquired(conn, name, service, handler),
        lambda conn, name: on_name_acquired(conn, name),
        lambda conn, name: on_name_lost(conn, name, loop),
    )

    # Start inactivity timer
    service._reset_inactivity_timer()

    # Start HTTP REST API server (optional — gracefully skip if port in use)
    http_server = None
    SpeechHTTPHandler.service = service
    try:
        http_server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", args.http_port), SpeechHTTPHandler,
        )
        threading.Thread(target=http_server.serve_forever, daemon=True).start()
        log.info("HTTP server listening on http://127.0.0.1:%d", args.http_port)
    except OSError as e:
        log.warning("HTTP server failed to start on port %d: %s (continuing without HTTP)", args.http_port, e)

    # Handle SIGTERM/SIGINT
    def _on_signal(signum):
        log.info("Received signal %d, shutting down", signum)
        if http_server:
            http_server.shutdown()
        service.shutdown()
        loop.quit()
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, _on_signal, signal.SIGTERM)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, _on_signal, signal.SIGINT)

    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        if http_server:
            http_server.shutdown()
        service.shutdown()
        Gio.bus_unown_name(owner_id)
        log.info("Exited cleanly")


if __name__ == "__main__":
    main()
