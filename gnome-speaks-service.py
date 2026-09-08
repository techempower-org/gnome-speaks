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
from injector import Injector

# The IBus backend is optional at runtime: a machine without the IBus typelib,
# or a partial install, must still start on ydotool rather than not at all.
try:
    from ibus_injector import IbusInjector, restore_prior_engine
except Exception as _ibus_exc:  # pragma: no cover - platform dependent
    IbusInjector = None

    def restore_prior_engine(reason="startup"):
        return False
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
import inspect as _inspect  # noqa: E402
# Older speech-to-cli has no stop_when on stt(); detect once, never guess.
_STT_HAS_STOP_WHEN = "stop_when" in _inspect.signature(stt_dispatch).parameters

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
# Config file (shared with prefs.js and the speech-to-cli engine). Writes go
# through _save_config_flag: serialized by _config_write_lock and published
# with an atomic rename, because prefs.js merges on write and a torn read
# there costs the user every key in the file.
# ---------------------------------------------------------------------------

CONFIG_PATH = os.path.expanduser("~/.config/speech-to-cli/config.json")
_config_write_lock = threading.Lock()

# ---------------------------------------------------------------------------
# The Chronicle — append-only ledger of everything said (both directions).
# One JSON object per line; local-only (never enters git). Entries are
# respeakable via POST /respeak, the Respeak D-Bus method, or "cast echo".
# ---------------------------------------------------------------------------

CHRONICLE_PATH = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "gnome-speaks", "chronicle.jsonl")
# Every read walks backwards from EOF and stops once it has what it was asked
# for, so the common case (the badge's 8, the submenu's 12) touches one block.
# Rotation is the other half: it bounds the case a tail-read cannot short —
# a filtered search that matches nothing still has to walk the whole ledger,
# and without a cap "the whole ledger" grows forever.
_CHRONICLE_MAX_BYTES = int(os.environ.get(
    "GNOME_SPEAKS_CHRONICLE_MAX_BYTES", 2 * 1024 * 1024))
_CHRONICLE_GENERATIONS = 1  # chronicle.jsonl + chronicle.jsonl.1
_CHRONICLE_BLOCK = 65536
_chronicle_lock = threading.Lock()
_chronicle_last_id = 0
_chronicle_id_seeded = False
# Rotation runs on its own thread and is single-flight; the speech path only
# ever sets this flag. Cooldown keeps a broken archive from spawning a thread
# per utterance.
_chronicle_archiving = threading.Event()
_chronicle_archive_retry_after = 0.0


def _chronicle_files():
    """HOT ledger files newest-first: the active one, then rotated generations.

    Bounded, and therefore the only thing the recent list ever reads. The cold
    archive is deliberately NOT here — see _chronicle_archive_path.
    """
    return [CHRONICLE_PATH] + [
        "%s.%d" % (CHRONICLE_PATH, i)
        for i in range(1, _CHRONICLE_GENERATIONS + 1)]


def _chronicle_archive_path():
    """The cold archive: append-only, never rotated, never deleted.

    The Chronicle's promise is everything that was ever said, so rotation
    moves the oldest generation in here instead of dropping it. Growth is
    ~11 MB/year at the observed rate, which is why only an explicit search
    (`q`) or an id lookup ever reads it — never the recent list.

    Derived from CHRONICLE_PATH at call time so there is one source of truth.
    """
    return os.path.join(os.path.dirname(CHRONICLE_PATH),
                        "chronicle-archive.jsonl")


def _iter_lines_reverse(path, block=None):
    """Yield complete lines newest-first, reading backwards from EOF.

    Holds one block plus the line straddling the block boundary — never the
    file. Partial trailing writes are impossible here (appends are one
    write() of one line under the lock), but a truncated final line would
    simply fail to parse and be skipped by the callers.
    """
    block = block or _CHRONICLE_BLOCK
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        carry = b""
        while pos > 0:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            chunk = f.read(step) + carry
            lines = chunk.split(b"\n")
            carry = lines.pop(0)  # may be partial; completed next iteration
            for line in reversed(lines):
                if line.strip():
                    yield line
        if carry.strip():
            yield carry


def _chronicle_scan_locked(match, want, include_archive=False):
    """Collect up to `want` entries that `match` accepts, newest-first.

    Caller holds _chronicle_lock. Walks the hot generations first so history
    survives a rollover, and returns the moment it has enough.

    include_archive appends the cold archive as the LAST place to look, for
    the two callers that must reach the whole history: an id lookup (Respeak
    of a months-old line) and an explicit text search. Both run off the main
    loop. The recent list never sets it.

    Entries are deduplicated by id, because a crash between "archived" and
    "rotated" can leave one line in both the archive and a hot generation.
    Only ids seen in the HOT files are remembered — that is where the overlap
    lives, and it keeps the set bounded by the rotation cap instead of by the
    size of the archive.
    """
    out = []
    hot_ids = set()
    hot = _chronicle_files()
    paths = (hot + [_chronicle_archive_path()]) if include_archive else hot
    for path in paths:
        is_hot = path in hot
        try:
            for raw in _iter_lines_reverse(path):
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                eid = entry.get("id")
                if isinstance(eid, int) and not isinstance(eid, bool):
                    if is_hot:
                        hot_ids.add(eid)
                    elif eid in hot_ids:
                        continue  # already returned from a hot generation
                if match(entry):
                    out.append(entry)
                    if len(out) >= want:
                        return out
        except FileNotFoundError:
            continue
        except OSError:
            log.warning("Chronicle read failed (%s)", path, exc_info=True)
            break
    return out


def _chronicle_newest_archived_id():
    """Highest id already in the cold archive, or None.

    The archive is append-only and its ids ascend (generations go in in order,
    and ids ascend globally — see _chronicle_seed_id_locked), so its newest
    line is its high-water mark. One bounded tail-read.
    """
    try:
        for raw in _iter_lines_reverse(_chronicle_archive_path()):
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            eid = entry.get("id")
            if isinstance(eid, int) and not isinstance(eid, bool):
                return eid
    except (FileNotFoundError, OSError):
        pass
    return None


def _chronicle_archive_generation(src):
    """Append `src` to the cold archive. True when its lines are safely there.

    Runs on the rotation thread WITHOUT _chronicle_lock: this is a copy of a
    whole generation, and holding the lock would put it in front of the next
    _chronicle_append — which is called from the main loop. Safe to run
    unlocked because rotation is single-flight (_chronicle_archiving), so
    nothing else writes `src` or the archive while this runs, and readers of
    either file are read-only.

    Lines already at or below the archive's high-water mark are skipped, so
    archiving the same generation twice is a no-op rather than a duplication.
    That matters because the step after this one can fail: if it does, `src`
    survives and will be offered again on the next rotation.
    """
    watermark = _chronicle_newest_archived_id()
    archive = _chronicle_archive_path()
    kept = skipped = 0
    try:
        with open(src, "rb") as fin, open(archive, "ab") as fout:
            for raw in fin:
                if not raw.strip():
                    continue
                if watermark is not None:
                    try:
                        eid = json.loads(raw).get("id")
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        eid = None
                    if (isinstance(eid, int) and not isinstance(eid, bool)
                            and eid <= watermark):
                        skipped += 1
                        continue
                if not raw.endswith(b"\n"):
                    raw += b"\n"  # never let two entries fuse into one line
                fout.write(raw)
                kept += 1
            fout.flush()
            os.fsync(fout.fileno())
    except FileNotFoundError:
        return True  # nothing to archive: this generation does not exist yet
    except OSError:
        # Refusing to rotate is the right failure: the active file grows past
        # its cap (slower searches) instead of history being deleted.
        log.warning("Chronicle archive append failed — rotation aborted",
                    exc_info=True)
        return False
    if kept or skipped:
        log.info("Chronicle archived %d line(s) (%d already archived)",
                 kept, skipped)
    return True


def _chronicle_rotate_worker():
    """Archive the oldest generation, then roll the generations.

    Order matters and is the reason this is safe to do in the background:
    the archive copy completes and is fsynced BEFORE the renames, so every
    line is readable from its hot generation for the whole copy and from the
    archive afterwards. There is no instant where a line is invisible — only
    a window where it is in both places, which reads deduplicate by id.
    """
    global _chronicle_archive_retry_after
    try:
        oldest = "%s.%d" % (CHRONICLE_PATH, _CHRONICLE_GENERATIONS)
        if not _chronicle_archive_generation(oldest):
            # Leave the generations alone: the active file grows past its cap
            # (slower searches) rather than history being deleted. Back off so
            # a persistently unwritable archive does not spawn a thread per
            # utterance.
            _chronicle_archive_retry_after = time.monotonic() + 60
            return
        with _chronicle_lock:
            for i in range(_CHRONICLE_GENERATIONS, 0, -1):
                src = (CHRONICLE_PATH if i == 1
                       else "%s.%d" % (CHRONICLE_PATH, i - 1))
                try:
                    os.replace(src, "%s.%d" % (CHRONICLE_PATH, i))
                except FileNotFoundError:
                    continue
                except OSError:
                    log.warning("Chronicle rotation failed", exc_info=True)
                    return
        log.info("Chronicle rotated past %d bytes", _CHRONICLE_MAX_BYTES)
    except Exception:
        log.warning("Chronicle rotation aborted", exc_info=True)
    finally:
        _chronicle_archiving.clear()


def _chronicle_rotate_locked():
    """Kick off a rotation if the active ledger has passed its cap.

    Detection only — the work happens on a background thread. Archiving a
    generation is a bounded copy, but "bounded" is _CHRONICLE_MAX_BYTES, and
    _chronicle_append runs on the GLib main loop (via
    _emit_transcription_ready): measured 4 ms at the 2 MB default and 117 ms
    with a 16 MB cap, all of it in front of the next utterance. Caller holds
    _chronicle_lock.
    """
    try:
        if os.path.getsize(CHRONICLE_PATH) < _CHRONICLE_MAX_BYTES:
            return
    except OSError:
        return
    if time.monotonic() < _chronicle_archive_retry_after:
        return
    if _chronicle_archiving.is_set():
        return  # single-flight: one rotation at a time, by design
    _chronicle_archiving.set()
    threading.Thread(target=_chronicle_rotate_worker, daemon=True,
                     name="chronicle-rotate").start()


def _chronicle_seed_id_locked():
    """Adopt the newest id on disk so ids keep ascending across restarts.

    _chronicle_last_id starts at 0 every run and ids are wall-clock ms, so a
    backwards clock step (NTP correction, suspended laptop) could mint an id
    that already exists in the ledger — and Respeak would then have two
    entries answering to one id. One bounded tail-read at the first append
    closes that. Caller holds _chronicle_lock.
    """
    global _chronicle_last_id, _chronicle_id_seeded
    _chronicle_id_seeded = True
    # include_archive: if the hot files were cleared by hand, the archive is
    # still the record of which ids exist — minting an id it already holds
    # would give Respeak two answers for one lookup.
    recent = _chronicle_scan_locked(
        lambda e: isinstance(e.get("id"), int) and not isinstance(e["id"], bool),
        50, include_archive=True)
    if recent:
        _chronicle_last_id = max([_chronicle_last_id]
                                 + [e["id"] for e in recent])
        log.debug("Chronicle ids resume from %d", _chronicle_last_id)


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
            os.makedirs(os.path.dirname(CHRONICLE_PATH), exist_ok=True)
            if not _chronicle_id_seeded:
                _chronicle_seed_id_locked()
            # ms timestamp as id; bump on same-ms collisions so ids stay
            # unique without a persistent counter.
            eid = int(time.time() * 1000)
            if eid <= _chronicle_last_id:
                eid = _chronicle_last_id + 1
            _chronicle_last_id = eid
            entry["id"] = eid
            with open(CHRONICLE_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _chronicle_rotate_locked()
    except OSError:
        log.warning("Chronicle append failed", exc_info=True)


def _chronicle_read(limit=20, q=None, kind=None, include_archive=False):
    """Return the newest matching entries, oldest-first (ready to display).

    include_archive extends the walk into the cold archive once the hot
    generations are exhausted. Only an explicit text search sets it: the
    recent list (the badge, the submenu, GetChronicle) must stay a bounded
    read of the hot files, and it is the one caller that cannot afford to
    walk a year of history to fill twelve rows.
    """
    want = max(1, min(int(limit or 20), 500))
    needle = q.lower() if q else None

    def _match(entry):
        if kind and entry.get("kind") != kind:
            return False
        if needle and needle not in entry.get("text", "").lower():
            return False
        return True

    with _chronicle_lock:
        entries = _chronicle_scan_locked(_match, want,
                                        include_archive=include_archive)
    entries.reverse()  # collected newest-first; the display wants oldest-first
    return entries


def _chronicle_find(entry_id):
    """Return the entry with this id, or None.

    Searches newest-first, so on the (clock-step) chance of a duplicate id
    the most recent entry wins rather than the most ancient. Falls through to
    the cold archive last: respeaking a line from months ago has to work, and
    this runs off the main loop (Respeak goes through the D-Bus pool).
    """
    try:
        entry_id = int(entry_id)
    except (TypeError, ValueError):
        return None
    with _chronicle_lock:
        hits = _chronicle_scan_locked(lambda e: e.get("id") == entry_id, 1,
                                     include_archive=True)
    return hits[0] if hits else None


# ---------------------------------------------------------------------------
# Clipboard helpers (our own — do not import from speech-to-cli)
# ---------------------------------------------------------------------------

def _run_first_available(cmds, timeout, label):
    """Run each of `cmds` in turn, yielding the result of every one that RAN.

    The genuinely shared part of the THREE selection readers: try the Wayland
    tool, fall through to the X11 one when it is not installed, give up on one
    that hangs.

    It YIELDS rather than deciding, because the callers do not agree on what
    counts as success and that test sits INSIDE the loop:

      clipboard_read       timeout 5, exit 0 wins outright, stdout VERBATIM, ""
      selection_read       timeout 5, ditto, PRIMARY selection
      _get_clipboard_text  timeout 1, exit-0-but-EMPTY means TRY THE NEXT
                           TOOL, result STRIPPED and truncated to 200, None

    Deciding here would need a per-caller flag, which is the tell that the
    thing was never shared. So the loop is shared and the verdict is not.
    """
    for cmd in cmds:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=timeout)
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            log.debug("%s read timed out: %s", label, cmd[0])
            continue
        yield result


_CLIPBOARD_CMDS = [["wl-paste", "--no-newline"],
                   ["xclip", "-selection", "clipboard", "-o"]]
_PRIMARY_CMDS = [["wl-paste", "--primary", "--no-newline"],
                 ["xclip", "-selection", "primary", "-o"]]


def clipboard_read():
    """Read text from the clipboard (Wayland-first, X11 fallback)."""
    for result in _run_first_available(_CLIPBOARD_CMDS, 5, "Clipboard"):
        if result.returncode == 0:
            return result.stdout
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


_YDOTOOLD_UNIT_CANDIDATES = ("ydotool.service", "ydotoold.service")


def _ydotoold_unit(proc_root="/proc"):
    """(scope, unit) of the systemd unit that owns the LIVE ydotoold, or None.

    Two units can exist on one machine -- the packaged user unit
    `ydotool.service` (/usr/bin/ydotoold) and fix-ydotool.sh's `ydotoold.service`
    (/usr/local/bin/ydotoold) -- and only one of them can hold the socket.
    Restarting the other one starts a second daemon that exits with "Another
    ydotoold is running with the same socket", trips the start limit, and
    never clears a stuck key (#102). So ask the kernel which unit owns the
    process: /proc/<pid>/exe (the executable, never a name pattern -- a cmdline
    scan matches its own shell) and /proc/<pid>/cgroup for the unit.
    scope is "user" or "system".
    """
    try:
        pids = [d for d in os.listdir(proc_root) if d.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            exe = os.readlink(os.path.join(proc_root, pid, "exe"))
        except OSError:
            continue
        if os.path.basename(exe) != "ydotoold":
            continue
        try:
            with open(os.path.join(proc_root, pid, "cgroup")) as fh:
                cg = fh.read()
        except OSError:
            continue
        m = re.search(r"/([^/\s]+\.service)\s*$", cg, re.M)
        if not m:
            continue
        scope = "user" if "/user.slice/" in cg or "user@" in cg else "system"
        return scope, m.group(1)
    return None


def _reset_ydotoold():
    """Restart ydotoold to clear any stuck key state on its virtual device.

    When a ydotool command is interrupted between a key-down and key-up event,
    the virtual uinput device retains that key as "pressed". The Wayland
    compositor then suppresses that key from all physical keyboards. Restarting
    the daemon destroys the old virtual device and creates a clean one.

    Restarts the unit that OWNS the live daemon (see _ydotoold_unit); with no
    daemon running, starts the first known unit that exists. Never restarts a
    unit by an assumed name -- that is how the reset became a no-op that left
    a failed unit behind (#102).

    Uses a lock to prevent two threads from restarting simultaneously.
    """
    if not _YDOTOOL_V1:
        return
    if not _ydotool_reset_lock.acquire(blocking=False):
        return  # another thread is already restarting
    try:
        owner = _ydotoold_unit()
        if owner is not None:
            scope, unit = owner
            if scope != "user":
                log.warning("ydotoold runs as system unit %s; cannot restart it "
                            "without privileges -- stuck keys need `sudo systemctl "
                            "restart %s`", unit, unit)
                return
            cmd = ["systemctl", "--user", "restart", unit]
        else:
            # No daemon at all: start (not restart) the first candidate that exists.
            unit = None
            for cand in _YDOTOOLD_UNIT_CANDIDATES:
                probe = subprocess.run(
                    ["systemctl", "--user", "cat", cand],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                if probe.returncode == 0:
                    unit = cand
                    break
            if unit is None:
                log.warning("No ydotoold running and no known unit to start")
                return
            cmd = ["systemctl", "--user", "start", unit]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5)
        # Give ydotoold time to create the new virtual device
        time.sleep(0.1)
        log.info("Reset ydotoold (%s) to clear stuck key state", unit)
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


# ---------------------------------------------------------------------------
# Injection seam
# ---------------------------------------------------------------------------
# Every path that puts text in front of the user's cursor goes through an
# Injector.  The contract lives in injector.py; the two backends are
# YdotoolInjector below (synthesized key events) and IbusInjector in
# ibus_injector.py (D-Bus text commits).
#
# YdotoolInjector delegates to the module-level functions above rather than
# absorbing them: it keeps their bodies byte-identical (so every timing quirk
# survives by construction) and keeps them patchable by name, which the test
# harnesses rely on to stay off the real uinput device.


class YdotoolInjector(Injector):
    """The original backend: ydotool (or xdotool), synthesizing key events.

    Thin adapter over the module-level typing functions above — same code,
    same timings, same quirks.
    """

    name = "ydotool"

    def prepare(self):
        _detect_typing_tool()
        _reset_ydotoold()

    def available(self):
        return _TYPING_TOOL in ("ydotool", "xdotool")

    def supports_preedit(self):
        return False

    def commit(self, text):
        return type_at_cursor(text)

    def type_text(self, text):
        return _type_raw(text)

    def press_enter(self):
        # Same underlying call as type_text for THIS backend only, because
        # ydotool types and presses keys through one tool. They stay separate
        # methods because no other backend can conflate them.
        return _type_raw("\n")

    def send_backspaces(self, count):
        return _send_backspaces(count)

    def replace_text(self, old_text, new_text):
        return replace_typed_text(old_text, new_text)

    def paste(self, text):
        return _clipboard_paste(text)

    def recover(self):
        return _reset_ydotoold()


_INJECTION_METHODS = ("ydotool", "ibus", "auto")

_injector = None
_injector_method = None
_injector_lock = threading.Lock()
# When "ibus"/"auto" resolved to the ydotool fallback because the daemon was
# not reachable (typically: the service started before ibus-daemon at login),
# remember when, so get_injector() can retry instead of caching the fallback
# for the whole session (#100). None = the cached backend is the one asked for.
_injector_fallback_since = None
_INJECTOR_RETRY_SECONDS = 15.0


def _make_injector():
    """Pick a backend from CONFIG['injection_method'].

    Falls back to ydotool for every failure: an unknown value, IBus bindings
    missing, the daemon unreachable, registration refused.  Never falls back
    to nothing — losing dictation to a misconfigured key is a worse outcome
    than ignoring the key.  (The key must also be whitelisted in
    speech-to-cli's state.py load_config(), or it reads "ydotool" forever.)
    """
    method = str(CONFIG.get("injection_method") or "ydotool").strip().lower()
    if method not in _INJECTION_METHODS:
        log.warning("Unknown injection_method %r (expected one of %s), using ydotool",
                    method, ", ".join(_INJECTION_METHODS))
        method = "ydotool"

    ydotool = YdotoolInjector()
    if method == "ydotool":
        return ydotool

    if IbusInjector is None:
        # "auto" resolving to ydotool because this machine has no IBus is the
        # designed outcome, not a fault — warning about it would cry wolf on
        # every non-GNOME session. An explicit "ibus" request is different:
        # the user asked for something they are not getting.
        if method == "ibus":
            log.warning("injection_method=ibus requested but the IBus backend "
                        "could not be imported; using ydotool")
        else:
            log.info("injection_method=auto: no IBus backend available, "
                     "using ydotool")
        return ydotool

    ibus = IbusInjector(fallback=ydotool)
    if ibus.available():
        return ibus
    if method == "ibus":
        log.warning("injection_method=ibus requested but IBus is unavailable; "
                    "using ydotool")
    else:
        log.info("injection_method=auto: IBus unavailable, using ydotool")
    return ydotool


_SPEECH_ROUTE_WORDS = {
    None: "Azure live",
    "forced": "forced offline: SPEECH_FORCE_OFFLINE is set, no Azure fallback",
    "prefer_local": "local preferred: speech_backend=local, Azure only as fallback",
    "azure_down": "Azure on cooldown after a failure",
}


def _speech_route_words(reason=None):
    """Human words for why Azure is (not) being used right now (#103)."""
    if reason is None:
        reason = wyoming_mod.skip_reason() if hasattr(wyoming_mod, "skip_reason") else None
    return _SPEECH_ROUTE_WORDS.get(reason, str(reason))


def speech_route():
    """Machine-readable routing state for GET /status and the startup log.

    backend        what config asks for: "azure" | "local"
    offline_reason None while Azure is live, else "forced" | "prefer_local" |
                   "azure_down" (see wyoming.skip_reason)
    local_down     the LAN server is on cooldown after a failure
    """
    reason = wyoming_mod.skip_reason() if hasattr(wyoming_mod, "skip_reason") else None
    return {
        "backend": str(CONFIG.get("speech_backend", "azure")).strip().lower(),
        "offline_reason": reason,
        "local_down": bool(getattr(wyoming_mod, "local_down", lambda: False)()),
        "forced_offline": bool(wyoming_mod.force_offline()),
        "wyoming_configured": bool(wyoming_mod.enabled()),
    }


def _speech_ready(need_azure=False):
    """Can the service speak or listen right now? None = yes, else the words
    for what is actually missing.

    The ONE owner of the "Azure Speech key not configured" verdict (#125). Six
    copy-pasted `if not CONFIG.get("key")` gates each refused a keyless install
    outright -- but with speech_backend=local (#108) or SPEECH_FORCE_OFFLINE the
    STT/TTS paths never touch Azure, so the key is not what is missing. The
    front door has to agree with the routing wyoming.skip_azure() applies
    downstream, or a complete offline install is refused at the door.

    need_azure=True is for the one caller that has no local path at all:
    Talk is speech_tts.talk_fullduplex(), which posts to Azure directly and
    streams Azure STT -- no Wyoming seam on either side.
    """
    if CONFIG.get("key"):
        return None
    if need_azure:
        return ("Azure Speech key not configured (Talk is full-duplex Azure "
                "and has no local speech path)")
    if not wyoming_mod.enabled():
        return ("Azure Speech key not configured and no local speech server "
                "(wyoming_host) set")
    if wyoming_mod.skip_azure():
        # forced / prefer_local / azure_down: the LAN server takes the call
        return None
    if getattr(wyoming_mod, "prefer_local", lambda: False)():
        # prefer_local, but the LAN server is on cooldown: the fallback IS Azure
        return ("local speech server on cooldown after a failure, and no Azure "
                "Speech key to fall back to")
    return ("Azure Speech key not configured -- set speech_backend=local to "
            "use the local speech server")


def get_injector():
    """The process-wide injection backend (lazy, rebuilt when config changes)."""
    global _injector, _injector_method, _injector_fallback_since
    method = str(CONFIG.get("injection_method") or "ydotool").strip().lower()
    if _injector is not None and method == _injector_method and not _fallback_retry_due(rearm=False):
        return _injector
    with _injector_lock:
        if _injector is not None and method == _injector_method and not _fallback_retry_due(rearm=True):
            return _injector
        previous = _injector
        if previous is not None:
            # Hand the desktop back before swapping backends underneath it.
            try:
                previous.cancel()
            except Exception:
                log.debug("Previous injector cancel failed", exc_info=True)
        _injector = _make_injector()
        _injector_method = method
        wanted_ibus = method in ("ibus", "auto") and IbusInjector is not None
        if wanted_ibus and getattr(_injector, "name", "") == "ydotool":
            if _injector_fallback_since is None:
                _injector_fallback_since = time.monotonic()
        else:
            _injector_fallback_since = None
        log.info("Injection backend: %s (injection_method=%s)",
                 _injector.name, method)
        return _injector


def _fallback_retry_due(rearm):
    """True when the cached backend is an unwanted ydotool fallback and the
    retry interval has passed. Bounded: one retry per interval, never a spin.

    The lock-free fast path in get_injector() asks with rearm=False (a pure
    read); only the caller holding _injector_lock rearms, so several callers
    arriving together cost one attempt, and the fast path can never rearm the
    clock out from under the caller about to rebuild.
    """
    global _injector_fallback_since
    since = _injector_fallback_since
    if since is None:
        return False
    if time.monotonic() - since < _INJECTOR_RETRY_SECONDS:
        return False
    if rearm:
        _injector_fallback_since = time.monotonic()
    return True


class _LiveTyper:
    """Type streaming hypotheses off the WebSocket receive thread.

    ydotool types at 12 ms/char and is awaited synchronously, so typing a
    hypothesis inline in the recv loop starves ws.recv(): a 20-char suffix
    is ~240 ms against Azure hypotheses every 100-300 ms.  Later hypotheses
    queue in the socket, each stale one is typed and then partially erased
    by the next, and the lag grows with speaking speed.  Here the recv loop
    only drops the newest hypothesis into a one-slot mailbox; this worker
    types the diff against whatever is newest when it gets there and never
    sees the intermediates.

    `typed_holder[0]` remains the on-screen source of truth for the final
    commit arithmetic.  Only the worker writes it while the cycle is open;
    read it only after close() has joined the worker.

    `injector` is the backend PINNED by the cycle that owns this worker, not
    `get_injector()`.  A "cast typing engine" mid-utterance rebuilds the
    process-wide backend, and the half-typed hypothesis on screen belongs to
    the old one -- resolving the backend here would retract it with the new
    one (#46).
    """

    def __init__(self, typed_holder, log_fn, injector):
        self._typed = typed_holder
        self._log = log_fn
        self._inj = injector
        self._cv = threading.Condition()
        self._pending = None
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="live-typer")
        self._thread.start()

    def submit(self, text):
        """Make `text` the newest hypothesis; overwrites any untyped one."""
        with self._cv:
            self._pending = text
            self._cv.notify()

    def close(self, timeout=5.0):
        """Stop the worker, dropping anything not yet typed.

        The caller reconciles the screen against the final transcript
        itself, so an untyped intermediate is wasted keystrokes, not lost
        text.  Blocks until the in-flight replace (if any) has finished.
        """
        with self._cv:
            self._closed = True
            self._pending = None
            self._cv.notify()
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._log("live typer still typing after close(); "
                      "typed_partial may be stale")

    def _run(self):
        while True:
            with self._cv:
                while self._pending is None and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                text, self._pending = self._pending, None
            if text == self._typed[0]:
                continue
            try:
                self._inj.replace_text(self._typed[0], text)
                self._typed[0] = text
            except Exception as exc:
                # Screen state is unknown now; keep the last value we are
                # sure of rather than claim `text` is on screen.
                self._log(f"live typing failed: {exc}")


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
    _pl = CONFIG.get("phrase_list", []) or []
    if isinstance(_pl, str):  # prefs stores an entry row as one string
        _pl = [p.strip() for p in _pl.split(",") if p.strip()]
    for phrase in _pl:
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
    for result in _run_first_available(_PRIMARY_CMDS, 5, "Selection"):
        if result.returncode == 0:
            return result.stdout
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


# Terminal-mode spoken numbers. Azure lexical spells digits as words; a
# terminal wants digits. Two composition styles, both deterministic:
#   digit-run:  "one two seven" -> 127   (spelled digit by digit)
#   small grammar: "twenty two" -> 22, "four hundred" -> 400 (< 1000)
# Runs feed the symbol pass afterwards, so "one two seven dot zero dot
# zero dot one" -> 127.0.0.1 and "port colon eight zero eight zero"
# -> port:8080.
_NUM_UNITS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3,
              "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
              "nine": 9}
_NUM_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
              "fourteen": 14, "fifteen": 15, "sixteen": 16,
              "seventeen": 17, "eighteen": 18, "nineteen": 19}
_NUM_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
             "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def _terminal_numbers(text):
    """Convert runs of spoken number words to digits (terminal mode)."""
    words = text.split()
    out, i = [], 0
    while i < len(words):
        w = words[i].lower()
        if w not in _NUM_UNITS and w not in _NUM_TEENS and w not in _NUM_TENS:
            out.append(words[i])
            i += 1
            continue
        # Collect the maximal run of number words.
        j = i
        run = []
        while j < len(words):
            lw = words[j].lower()
            if lw in _NUM_UNITS or lw in _NUM_TEENS or lw in _NUM_TENS or \
                    (lw == "hundred" and run):
                run.append(lw)
                j += 1
            else:
                break
        # Style 1: all single digits -> concatenate (127, 8080, versions).
        if all(r in _NUM_UNITS for r in run):
            out.append("".join(str(_NUM_UNITS[r]) for r in run))
        else:
            # Style 2: small-number grammar, segmenting greedily.
            # "twenty two" -> 22; "one hundred five" -> 105;
            # "twenty twenty six" -> 2026 (segments concatenate).
            segs, cur, k = [], None, 0
            while k < len(run):
                r = run[k]
                if r == "hundred":
                    cur = (cur if cur is not None else 1) * 100
                elif r in _NUM_TENS:
                    if cur is not None and cur % 100 == 0:
                        cur += _NUM_TENS[r]
                    else:
                        if cur is not None:
                            segs.append(cur)
                        cur = _NUM_TENS[r]
                elif r in _NUM_TEENS:
                    if cur is not None and cur % 100 == 0:
                        cur += _NUM_TEENS[r]
                    else:
                        if cur is not None:
                            segs.append(cur)
                        cur = _NUM_TEENS[r]
                else:  # unit
                    if cur is not None and cur % 10 == 0 and cur % 100 != 0:
                        cur += _NUM_UNITS[r]
                        segs.append(cur)
                        cur = None
                    elif cur is not None and cur % 100 == 0:
                        cur += _NUM_UNITS[r]
                        segs.append(cur)
                        cur = None
                    else:
                        if cur is not None:
                            segs.append(cur)
                        cur = _NUM_UNITS[r]
                k += 1
            if cur is not None:
                segs.append(cur)
            out.append("".join(str(s) for s in segs))
        i = j
    return " ".join(out)


# Terminal-mode spoken symbols. Azure's lexical form (terminal mode) spells
# symbols as words — "claude hyphen hyphen resume" — and the prose table in
# _VOICE_COMMANDS is wrong for a shell ("dash" becomes an em-dash, "- -"
# never fuses). This pass converts the shell alphabet with join rules:
#   both  — glue to both neighbours:  example dot com -> example.com
#   right — keep the space before, glue after:  ls hyphen la -> ls -la
# Adjacent symbols always fuse, so "hyphen hyphen resume" -> --resume.
# "enter"/"new line" are DELIBERATELY absent: a misheard word must never
# be able to execute a command.
_TERM_TWO_WORD = {
    ("hyphen", "hyphen"): ("--", "right"), ("dash", "dash"): ("--", "right"),
    ("at", "sign"): ("@", "both"), ("question", "mark"): ("?", "right"),
    ("forward", "slash"): ("/", "both"), ("back", "slash"): ("\\", "both"),
    ("open", "paren"): ("(", "right"), ("close", "paren"): (")", "both"),
    ("open", "bracket"): ("[", "right"), ("close", "bracket"): ("]", "both"),
    ("open", "brace"): ("{", "right"), ("close", "brace"): ("}", "both"),
    ("less", "than"): ("<", "right"), ("greater", "than"): (">", "right"),
    ("double", "quote"): ('"', "right"), ("single", "quote"): ("'", "right"),
    ("dollar", "sign"): ("$", "right"), ("percent", "sign"): ("%", "right"),
    ("exclamation", "mark"): ("!", "right"), ("exclamation", "point"): ("!", "right"),
    ("vertical", "bar"): ("|", "right"),
}
_TERM_ONE_WORD = {
    "dot": (".", "both"), "period": (".", "both"),
    "slash": ("/", "both"), "backslash": ("\\", "both"),
    "hyphen": ("-", "right"), "dash": ("-", "right"), "minus": ("-", "right"),
    "underscore": ("_", "both"), "equals": ("=", "both"),
    "colon": (":", "both"), "semicolon": (";", "right"),
    "comma": (",", "right"),
    "tilde": ("~", "right"), "pipe": ("|", "right"),
    "star": ("*", "right"), "asterisk": ("*", "right"),
    "ampersand": ("&", "right"), "percent": ("%", "right"),
    "hash": ("#", "right"), "dollar": ("$", "right"),
    "caret": ("^", "right"), "backtick": ("`", "right"),
    "bang": ("!", "right"),
}


def _terminal_symbols(text):
    """Convert spoken symbol words to characters with shell join rules."""
    words = text.split()
    out = []            # list of (chunk, is_symbol, glue)
    i = 0
    while i < len(words):
        w = words[i].lower()
        two = (w, words[i + 1].lower()) if i + 1 < len(words) else None
        if two in _TERM_TWO_WORD:
            ch, glue = _TERM_TWO_WORD[two]
            out.append((ch, True, glue))
            i += 2
            continue
        if w in _TERM_ONE_WORD:
            ch, glue = _TERM_ONE_WORD[w]
            out.append((ch, True, glue))
            i += 1
            continue
        out.append((words[i], False, None))
        i += 1

    result = []
    glue_next = False
    prev_symbol = False
    for chunk, is_symbol, glue in out:
        joined = glue_next or (is_symbol and (glue == "both" or prev_symbol))
        if result and not joined:
            result.append(" ")
        result.append(chunk)
        glue_next = is_symbol           # any symbol glues to what follows
        prev_symbol = is_symbol
    return "".join(result)


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

class CancelToken:
    """One cancellable operation's private verdict.

    speech-to-cli exposes exactly one cancellation channel -- the process-wide
    ``state._cancel_event`` -- and every library call polls it.  A single bit
    cannot say *which* operation was cancelled, so the worker that starts next
    used to erase the cancel the previous one was still obeying (each worker
    opened with ``state._cancel_event.clear()``).  A token splits the two jobs
    the one bit was doing:

      * the global event stays the **wire**: the only thing that can interrupt
        a library call already in flight.  It is still set and cleared, and its
        meaning is unchanged for speech-to-cli's own CLI consumers.
      * the token is the **verdict**: set once, never reset, owned by exactly
        one operation.  Every decision the service makes about a finished
        operation -- type this transcript, record this outcome, restart the
        loop -- reads the token, so no later worker can un-cancel it.

    Same ownership shape as ``_speak_token``: the object identity *is* the
    claim, and a worker acts only on the token it was handed.
    """

    __slots__ = ("id", "label", "_event")

    def __init__(self, token_id, label):
        self.id = token_id
        self.label = label
        self._event = threading.Event()

    @property
    def cancelled(self):
        return self._event.is_set()

    def cancel(self):
        self._event.set()

    def wait_cancelled(self, timeout=None):
        return self._event.wait(timeout)

    def __repr__(self):
        mark = " cancelled" if self.cancelled else ""
        return f"<CancelToken {self.label}#{self.id}{mark}>"


class CancelRegistry:
    """Issues cancel tokens and projects them onto the one global wire.

    Lifecycle of an operation:

        token = registry.issue("stt")     # registered; cancellable from now on
        if not registry.begin(token):     # take the wire -- False if already
            ...                           #   cancelled, so never even start
        try:
            ...                           # library call polls the wire
        finally:
            registry.retire(token)        # deregister; drop the wire if ours

    ``issue`` deliberately does NOT touch the wire.  That closes the window in
    which an operation is visible to /stop but has not yet started: a cancel
    arriving there is remembered by the token, and ``begin`` then refuses.

    Only ``begin`` lowers the wire, and only for an operation that has not been
    cancelled -- so an operation can never un-cancel *itself*.  It can still
    lower a wire another live-but-cancelled operation was watching; that is the
    irreducible cost of one shared bit, and it is exactly what the verdict is
    for.  The warning logged there is the trace for "a worker outlived its
    stop".

    Lock order: the registry never calls back into the service, so its lock is
    always innermost (``_queue_current_lock -> registry`` is safe).

    This class is the ONLY place in the service that touches
    ``state._cancel_event``.  Everywhere else reads a token, so
    ``grep -n "_cancel_event" gnome-speaks-service.py`` returning only this
    class and prose is a usable check that no verdict is being taken from the
    wire again (issue #42).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._live = {}
        self._owner = None
        self._next_id = 0

    def issue(self, label):
        """Register a new operation. Cancellable immediately; not yet running."""
        with self._lock:
            self._next_id += 1
            token = CancelToken(self._next_id, label)
            self._live[token.id] = token
            return token

    def begin(self, token):
        """Hand the wire to this operation. False = cancelled before it began."""
        with self._lock:
            if token.cancelled:
                return False
            stale = [t for t in self._live.values()
                     if t is not token and t.cancelled]
            self._owner = token
            state._cancel_event.clear()
        if stale:
            log.warning("cancel: %r took the wire while %s still winding down "
                        "(their verdicts stand)", token,
                        ", ".join(repr(t) for t in stale))
        return True

    def retire(self, token):
        """Deregister a finished operation; never leave a stale cancel behind."""
        if token is None:
            return
        with self._lock:
            self._live.pop(token.id, None)
            if self._owner is token:
                self._owner = None
                state._cancel_event.clear()

    def cancel(self, token, kill_procs=True):
        """Record one operation's verdict and raise the wire."""
        if token is not None:
            token.cancel()
        self._raise_wire(kill_procs)

    def cancel_all(self, kill_procs=True):
        """Record every live operation's verdict and raise the wire.

        Verdicts are written BEFORE the wire goes up and before any join, so a
        worker that outlives its stop can still tell that it was stopped.
        """
        with self._lock:
            tokens = list(self._live.values())
        for token in tokens:
            token.cancel()
        self._raise_wire(kill_procs)
        return tokens

    def live(self):
        with self._lock:
            return list(self._live.values())

    @staticmethod
    def _raise_wire(kill_procs):
        if kill_procs:
            state.cancel_active()      # sets the wire AND terminates procs
        else:
            state._cancel_event.set()


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


# Frames the kernel pipe between the recorder and this process can hold:
# 65536 B / 960 B = 68 frames = 2.048 s of 16 kHz mono PCM (measured with
# F_GETPIPE_SZ). Used as the loop-mode carry-over bound below, so a tapped
# session keeps the same amount of inter-utterance audio the pipe used to
# keep on its own.
_PIPE_FRAMES = 65536 // FRAME_BYTES


class _RecorderTap:
    """Owns a recorder's stdout: a reader thread drains the pipe into memory
    from the moment recording starts, independent of what the session is
    doing upstream.

    Why this exists (#49): the pipe holds 2.048 s of audio and a WebSocket
    connect attempt can last 10 s. With nothing reading, the recorder blocks
    once the pipe fills and the audio of that interval is lost AT THE SOURCE
    -- no downstream handoff can recover what was never captured. The tap
    keeps the recorder unblocked, so a connect that ultimately fails can
    still hand the *whole* utterance to the offline recognizer.

    Consumers read frames exactly as they read the pipe before: the tap
    exposes `.stdout` (itself) and a `read(FRAME_BYTES)` that returns one
    frame, or b"" at EOF -- so `calibrate_noise(tap)` and the sender loop are
    unchanged apart from the object they read from. EOF still arrives as a
    short read AFTER the buffer drains, which is what #57/#48 wants: the
    words already captured are delivered, then the lost mic is reported.
    """

    def __init__(self, proc, frame_bytes=FRAME_BYTES, max_frames=None):
        self._proc = proc
        self._frame_bytes = frame_bytes
        if max_frames is None:
            # The honest bound. It has to cover a whole utterance PLUS the
            # longest connect window in front of it: 4 attempts x a 10 s
            # websocket connect timeout, plus 1+2+4 s of backoff = 47 s.
            # 90 s at 16 kHz mono is ~2.9 MB, and beyond it the OLDEST frames
            # go (counted in .dropped and logged) rather than the newest.
            max_frames = int((MAX_LISTEN_SECONDS + 60) * 1000 / FRAME_MS)
        self._buf = deque(maxlen=max_frames)
        self._cv = threading.Condition()
        self._eof = False
        self._closed = False
        self._dropped = 0
        self._thread = threading.Thread(
            target=self._pump, name="rec-tap", daemon=True)
        self._thread.start()

    # -- producer ---------------------------------------------------------
    def _pump(self):
        try:
            while not self._closed:
                chunk = self._proc.stdout.read(self._frame_bytes)
                if not chunk or len(chunk) < self._frame_bytes:
                    break
                with self._cv:
                    if len(self._buf) == self._buf.maxlen:
                        self._dropped += 1
                    self._buf.append(chunk)
                    self._cv.notify()
        except Exception as exc:
            log.debug("Recorder tap ended: %s", exc)
        finally:
            with self._cv:
                self._eof = True
                self._cv.notify_all()

    # -- consumer ---------------------------------------------------------
    @property
    def stdout(self):
        """calibrate_noise() takes a proc and reads proc.stdout."""
        return self

    def read(self, _n=None, timeout=10.0):
        """One frame, blocking; b"" at EOF. Mirrors proc.stdout.read(n).

        A live recorder delivers a frame every 30 ms, so the timeout only
        fires when it has genuinely stalled -- where reading the pipe would
        have blocked forever. Reported as EOF, and logged so a stall is
        distinguishable from a recorder that simply exited. Callers must not
        read a lost mic off this b"" alone: `proc.poll()` is the positive
        control (#57), and it stays valid because the tap never touches proc.
        """
        with self._cv:
            while not self._buf:
                if self._eof or self._closed:
                    return b""
                if not self._cv.wait(timeout=timeout):
                    log.warning("Recorder tap: no audio for %.0fs -- "
                                "treating as end of stream", timeout)
                    return b""
            return self._buf.popleft()

    def drain(self):
        """Every frame buffered right now, oldest first. Never blocks."""
        with self._cv:
            out = list(self._buf)
            self._buf.clear()
            return out

    def trim(self, keep):
        """Drop all but the newest `keep` frames. Returns how many went.

        Called between loop-mode cycles: audio recorded while the previous
        utterance was being processed (and its reply spoken) is not part of
        the next one, and buffering all of it would feed the recognizer the
        service's own TTS.
        """
        with self._cv:
            excess = max(0, len(self._buf) - keep)
            for _ in range(excess):
                self._buf.popleft()
            return excess

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    @property
    def dropped(self):
        return self._dropped


class GnomeSpeaksService:
    """Core service logic using speech-to-cli building blocks."""

    STATES = ("idle", "listening", "processing", "speaking")

    def __init__(self):
        self._state = "idle"
        self._state_lock = threading.Lock()

        # Per-operation cancellation. The registry owns the verdicts; the
        # process-global state._cancel_event stays the wire (see CancelToken).
        self._cancels = CancelRegistry()

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

        # Inactivity timer (touched from every worker thread via _set_state)
        self._inactivity_source_id = None
        self._inactivity_lock = threading.Lock()
        self._inactivity_gen = 0
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
        self._queue_token = None                  # its CancelToken; same lock
        self._queue_current_lock = threading.Lock()
        # Refcounted, NOT a plain flag: user-speech paths overlap (a second
        # Speak preempting the first, a spell's Talk during an AI reply), and
        # a boolean would let the first one to finish unhold the queue while
        # the other is still playing. The Event is the dispatcher's cheap
        # read; _user_speech_depth is the truth. See _hold_user_speech.
        self._user_speech_active = threading.Event()
        self._user_speech_depth = 0
        self._user_speech_lock = threading.Lock()
        # Playback ownership token: each playback claims a fresh object();
        # a preempted worker's cleanup must not reset state it no longer owns.
        self._speak_token = None
        # Terminal outcome per queue item (done/canceled/interrupted/error),
        # exposed on GET /queue so the /speak id isn't write-only. Exactly one
        # terminal outcome per item (Android-style invariant).
        self._queue_recent = deque(maxlen=16)
        # Bumped by every drain. The dispatcher captures it before dequeuing
        # and re-reads it once it has the item, so a /stop that lands in the
        # gap between "off the queue" and "playing" still kills the item.
        # Plain int: increments race benignly (any bump is a mismatch).
        self._drain_gen = 0
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
    #
    # This is a SECOND config whitelist, and it must agree with the first:
    # every key here must also be in speech-to-cli's state.load_config(), or it
    # reaches CONFIG only through this side door -- which start_listening(
    # quick=True) (a wake-word-first session) never opens (#127). And every key
    # here must have a Python reader: extension.js-only keys were synced for
    # nobody. tests/repros/config-keys/verify_config_key_contract.py asserts
    # both (checks B and D; #120, #127).
    _SYNC_FLAGS = (
        # Speech provider (a STRING, applied verbatim -- the loop below does no
        # bool cast): "azure" | "local". Prefs flips it; a running service must
        # follow without a restart (#104).
        "speech_backend",
        # Mode flags
        "conversation_mode", "continuous_dictation", "dictation_mode",
        "terminal_mode", "skip_final_paste", "read_notifications",
        "wake_word", "wake_word_model", "llm_thinking",
        # Backend escape hatch: editing this key alone must be enough to get
        # off IBus, because the reason for getting off IBus may be that the
        # user cannot type.
        "injection_method",
        "speed", "pitch", "volume", "chronicle", "wake_word_secure_gate",
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
        # NOT the `show_*` visual toggles: only extension.js reads those (raw
        # config.json via _getConfigFlag); the service never does (#120, #127).
        # Debug
        "debug",
    )

    # How often the main loop polls CONFIG_PATH's mtime (#124). A stat every
    # 2 s is the whole cost; the parse only runs when the file changed.
    CONFIG_WATCH_SECONDS = 2

    def _reload_config_flags(self):
        """Re-read boolean mode flags from config file so prefs changes take effect.

        Skips JSON parse if file mtime is unchanged since last read.

        Two callers: the non-quick hotkey path in start_listening(), and the
        main-loop poll started by _start_config_watch() (#124). The poll is
        what makes prefs.js's "most settings apply live" true -- before it, a
        speech_backend / wake_word / chronicle flip sat on disk until the next
        NON-quick hotkey press, and wake-opened and loop sessions are quick.
        """
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
            if mtime == self._config_mtime:
                return
            with open(CONFIG_PATH) as f:
                disk = json.load(f)
            # Cache the mtime only once the parse succeeded: caching it first
            # makes a failed read look like an up-to-date one, and the
            # prefs change is then never applied.
            self._config_mtime = mtime
            for key in self._SYNC_FLAGS:
                if key in disk:
                    CONFIG[key] = disk[key]
        except Exception as exc:
            log.debug("Config reload skipped: %s", exc)

    def _start_config_watch(self):
        """Poll CONFIG_PATH from the main loop so prefs changes land without a
        hotkey press (#124). Returns the GLib source id.

        A timer, not a Gio.FileMonitor: prefs.js and _save_config_flag both
        publish by rename, but a monitor still fires per event and a parse
        that fails mid-write would not be retried until the next one. The
        mtime gate in _reload_config_flags already makes polling free.
        """
        def _tick():
            self._reload_config_flags()
            return GLib.SOURCE_CONTINUE
        return GLib.timeout_add_seconds(self.CONFIG_WATCH_SECONDS, _tick)

    def _save_config_flag(self, key, value):
        """Write a single flag back to the config file so prefs stays in sync.

        Read-modify-write under _config_write_lock, published with an atomic
        rename. Both halves matter:

        - the lock keeps two service threads (a D-Bus toggle and an HTTP
          spell, say) from each merging onto the same base and dropping one
          another's flag;
        - the rename keeps readers out of the write. open(path, "w")
          truncates in place, and prefs.js's merge-on-write re-reads this
          file — on a parse failure it falls back to `onDisk = {}` and
          writes back a config.json holding only the key it was editing,
          taking the Azure credentials with it.
        """
        CONFIG[key] = value
        try:
            with _config_write_lock:
                try:
                    with open(CONFIG_PATH) as f:
                        disk = json.load(f)
                    if not isinstance(disk, dict):
                        raise ValueError("config.json is not an object")
                except FileNotFoundError:
                    disk = {}
                disk[key] = value
                tmp = f"{CONFIG_PATH}.tmp.{os.getpid()}"
                os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
                try:
                    with open(tmp, "w") as f:
                        json.dump(disk, f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, CONFIG_PATH)
                except Exception:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
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
        if new_state == "idle":
            # Idle means no utterance is in flight, so it is the one place
            # every path — streaming, single-shot, conversation, error — is
            # guaranteed to pass through. end() is idempotent and flushes any
            # coalesced commit, so the IBus backend hands the user's input
            # method back here rather than depending on each call site to
            # remember. The watchdog covers the case where even this is missed.
            try:
                get_injector().end()
            except Exception:
                log.debug("Injector end() failed", exc_info=True)
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
        """Restart the idle-exit timer. Called from every _set_state, i.e.
        from worker threads that transition state concurrently.

        Serialized: read-remove-add on _inactivity_source_id used to race,
        and two threads landing on the same id both removed it (GLib
        "Source ID N was not found") and then both stored theirs — orphaning
        a live 10-minute timer per collision. Each orphan later fires, and
        a fire that finds the service idle quits the main loop, so the
        service exits while it is still in use. Measured: 1144 orphaned
        timers from 3s of two-thread state churn.
        """
        with self._inactivity_lock:
            if self._inactivity_source_id is not None:
                GLib.source_remove(self._inactivity_source_id)
                self._inactivity_source_id = None
            if INACTIVITY_TIMEOUT_SEC <= 0:
                return
            self._inactivity_gen += 1
            self._inactivity_source_id = GLib.timeout_add_seconds(
                INACTIVITY_TIMEOUT_SEC, self._on_inactivity_timeout,
                self._inactivity_gen,
            )

    def _on_inactivity_timeout(self, gen):
        with self._inactivity_lock:
            if gen != self._inactivity_gen:
                # Superseded while we sat in the main-loop dispatch queue:
                # not ours to act on, and must not clobber the live handle.
                return False
            self._inactivity_source_id = None  # one-shot; already spent
        if self.current_state == "idle":
            log.info("Inactivity timeout reached, quitting.")
            if self._main_loop is not None:
                self._main_loop.quit()
            return False
        self._reset_inactivity_timer()
        return False

    # -- STT: Streaming WebSocket using speech-to-cli building blocks ------

    def start_listening(self, quick=False, wake=False):
        """Start microphone recording with STT. Returns 'ok' or error string.

        wake=True marks a session opened by the wake word (hands-free, no
        deliberate hotkey press) — see _wake_gate_blocks.

        If quick=True, skip config reload and audio detection refresh.
        Used for tight loop restarts where config hasn't changed.
        """
        # A quick restart continues the SAME session (loop cycle, AI+Loop
        # retry), so it inherits how that session was opened; only a fresh
        # start -- the hotkey -- clears the wake mark (#55).
        if wake:
            self._wake_initiated = True
        elif not quick:
            self._wake_initiated = False
        # Prevent concurrent STT threads from rapid clicks -- and from the
        # "stop, it didn't stop, press again" sequence: stop() keeps the
        # reference to a worker that outlived its join, so a second worker is
        # never spawned beside one that still owns the recorder and the WS.
        # For quick (loop) restarts, briefly wait for the old thread to finish
        # since the loop restart fires before the thread fully exits.  A
        # worker restarting from its own thread must not join itself.
        me = threading.current_thread()
        with self._stt_lock:
            prev = self._stt_thread
        if prev is not None and prev is not me and prev.is_alive():
            if quick:
                prev.join(timeout=1.0)
            if prev.is_alive():
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

        # Azure marked down (60 s breaker) or SPEECH_FORCE_OFFLINE: the
        # streaming path has no offline seam of its own, so the session goes
        # to the batch path, which carries the Wyoming fallback. Without this
        # every press during an outage paid the full WS backoff and lost the
        # speech (#49).
        if mode == "streaming" and wyoming_mod.skip_azure():
            mode = "vad" if HAS_VAD else "fixed"
            log.info("Skipping Azure STT (%s) — routing to %s via the local Wyoming server",
                     _speech_route_words(), mode)

        # Use non-streaming STT backends (whisper, vad, fixed)
        if mode in ("whisper", "vad", "fixed"):
            if mode == "whisper" and not HAS_WHISPER:
                GLib.idle_add(self._emit_error, "faster-whisper not installed")
                return "error: no whisper support"
            if mode != "whisper":
                missing = _speech_ready()
                if missing:
                    GLib.idle_add(self._emit_error, missing)
                    return "error: speech not configured"

            self._stop_event.clear()
            self._set_state("listening")

            # Issued before the thread exists: a stop() arriving in the gap
            # between here and the worker's first instruction is remembered by
            # the token, and the worker then refuses to start.
            with self._stt_lock:
                self._stt_thread = threading.Thread(
                    target=self._batch_stt_worker,
                    args=(mode, self._cancels.issue("stt")),
                    daemon=True,
                )
                self._stt_thread.start()
            return "ok"

        # Streaming mode (default)
        missing = _speech_ready()
        if missing:
            GLib.idle_add(self._emit_error, missing)
            return "error: speech not configured"

        if not HAS_WS:
            GLib.idle_add(self._emit_error, "websocket-client not installed")
            return "error: no websocket support"

        self._stop_event.clear()
        self._set_state("listening")

        with self._stt_lock:
            self._stt_thread = threading.Thread(
                target=self._streaming_stt_worker,
                args=(self._cancels.issue("stt-stream"),),
                daemon=True,
            )
            self._stt_thread.start()
        return "ok"

    def _idle_after_stt(self):
        """Return to idle only if the mic path still owns the state.

        The state fence for STT, mirroring _speak_token's for playback: a
        worker that outlived its stop must not pull a later utterance out of
        "speaking" on its way out.
        """
        with self._state_lock:
            owned = self._state in ("listening", "processing")
        if owned:
            self._set_state("idle")

    @staticmethod
    def _reap_recorder(proc):
        """Kill a recorder process and stop tracking it.

        pw-record ignores SIGTERM -- escalate to SIGKILL after a short wait.
        Safe to call on a process that already exited.
        """
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

    def _batch_stt_worker(self, mode, cancel_token=None):
        """Background thread: batch STT using stt() dispatcher (whisper, vad, fixed).

        cancel_token is this listening session's verdict. It is consulted after
        stt_dispatch returns, because the library cannot be trusted to have
        seen the cancel: stt_fixed() checks is_cancelled() exactly once and
        then POSTs to Azure with a 30s timeout, and any worker that started in
        the meantime has taken the wire down. Without this check a transcript
        lands at the cursor after the user asked for silence.
        """
        if cancel_token is None:
            cancel_token = self._cancels.issue("stt")
        started = self._cancels.begin(cancel_token)
        try:
            if not started:
                log.info("STT cancelled before it began")
                GLib.idle_add(self._emit_transcription_ready, "")
                self._idle_after_stt()
                return
            # stop_when: the dictation hotkey / a loop-mode badge tap sets
            # _stop_event, and the batch (VAD) recorder must FINISH on it --
            # keep the words, return -- not run on until silence or 30 s. The
            # cancel wire is deliberately not used here: that abandons (#110).
            if _STT_HAS_STOP_WHEN:
                result = stt_dispatch(mode=mode, stop_when=self._stop_event.is_set)
            else:
                result = stt_dispatch(mode=mode)
            self._deliver_stt_result(result, mode, cancel_token)
        except Exception as exc:
            log.exception("Batch STT (%s) failed: %s", mode, exc)
            GLib.idle_add(self._emit_error, f"STT failed: {exc}")
            self._idle_after_stt()
            _schedule_warmup()
        finally:
            self._cancels.retire(cancel_token)

    def _deliver_stt_result(self, result, mode, cancel_token):
        """Route one finished batch-STT result: spellbook, LLM, cursor, or
        clipboard, then idle/loop. Shared by the batch worker and the
        streaming path's offline fallback."""
        if cancel_token.cancelled or result.get("cancelled"):
            log.info("STT cancelled — transcript discarded (%d chars)",
                     len(result.get("text") or ""))
            GLib.idle_add(self._emit_transcription_ready, "")
            self._idle_after_stt()
            _schedule_warmup()
            return

        # stt_vad()/stt_fixed() report a TOTAL failure -- Azure unreachable and
        # the Wyoming fallback failed too, or the recorder died -- as
        # {"error": ...} with no "text" key at all. Reading only "text" turned
        # that into "No speech detected" and a silent idle (#130); with
        # speech_backend=local every streaming dictation is routed here, so
        # this is the primary offline path, not a corner. Toast it like the
        # worker's own exception path does.
        error = result.get("error")
        if error:
            log.error("Batch STT (%s) failed: %s", mode, error)
            GLib.idle_add(self._emit_error, f"STT failed: {error}")
            GLib.idle_add(self._emit_transcription_ready, "")
            self._idle_after_stt()
            _schedule_warmup()
            return

        user_text = result.get("text", "")

        self._set_state("processing")

        # Spell incantations ("cast …") short-circuit typing/LLM routing;
        # matched on the raw transcript before punctuation substitution.
        if user_text and self._try_cast(user_text):
            GLib.idle_add(self._emit_transcription_ready, user_text)
            self._idle_after_stt()
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
                if not self._wake_gate_blocks():
                    get_injector().commit(user_text)
            else:
                clipboard_write(user_text)
        else:
            log.info("No speech detected (%s)", mode)
            GLib.idle_add(self._emit_transcription_ready, "")

        self._idle_after_stt()
        _schedule_warmup()

        if user_text and CONFIG.get("continuous_dictation", False) and not self._stop_event.is_set():
            GLib.idle_add(self._restart_listening_cb(
                lambda: (CONFIG.get("continuous_dictation", False)
                         and not self._stop_event.is_set())))

    def _report_recorder_dead(self, cycle):
        """The single place the lost-microphone verdict reaches the user.

        Reported ONCE per session and only after the words already captured
        have been delivered -- in addition to the text, never instead of it
        (#57/#48). Both session exits go through here so the wording, and the
        once-ness, cannot drift apart.
        """
        log.warning("Recorder exited mid-session (cycle %d) -- microphone lost",
                    cycle)
        GLib.idle_add(self._emit_error,
                      "Microphone disconnected — plug it in and press the hotkey")

    def _offline_stt_session(self, proc, tap, cancel_token, stopping, _log,
                             reason=None, recorder_dead=None):
        """Finish a streaming session whose WebSocket never came up.

        Takes over the recorder's tap and hands the audio to
        _rest_stt_fallback, which goes straight to Wyoming while Azure is
        marked down. `proc` is here ONLY as the liveness control (poll());
        the recorder is reaped by the cycle's teardown envelope, which owns
        it.

        ORDER MATTERS. The backlog the tap already holds -- everything
        recorded since the recorder started, including the whole connect
        attempt -- is drained FIRST and unconditionally. A stop request means
        "finish this utterance", not "throw it away": stop_listening() (the
        dictation hotkey) is the normal way a press ends while a connect is
        still pending, and it wants the words. Only stop() discards, it does
        so by cancelling the token, and that verdict is applied once, in
        _deliver_stt_result. Consulting stopping() before the drain handed
        the recognizer nothing but the calibration frames.
        """
        try:
            # Reads the head of the backlog (already buffered -- never
            # blocks) and gives those frames back, so the utterance stays in
            # order.
            energy_threshold, cal_frames = calibrate_noise(tap)
            frames = list(cal_frames) + tap.drain()
        except Exception as exc:
            log.exception("Offline STT drain failed: %s", exc)
            energy_threshold, frames = 500.0, []
        _log(f"offline: {len(frames)} frames buffered before handoff "
             f"({len(frames) * FRAME_MS / 1000.0:.2f}s)"
             + (f", {tap.dropped} dropped" if tap.dropped else ""))
        try:
            vad = webrtcvad.Vad(state.VAD_AGGRESSIVENESS) if HAS_VAD else None
            max_silence = int(state.SILENCE_TIMEOUT * 1000 / FRAME_MS)
            max_no_speech = int(state.NO_SPEECH_TIMEOUT * 1000 / FRAME_MS)
            min_speech = int(state.MIN_SPEECH_DURATION * 1000 / FRAME_MS)
            max_frames = int(MAX_LISTEN_SECONDS * 1000 / FRAME_MS)
            silence_frames = speech_frames = 0
            # Seed the VAD counters from the backlog: the utterance may
            # already be over (spoken and finished during a 10 s connect),
            # and a loop starting from zero would then sit through the
            # no-speech timeout before agreeing.
            for _f in frames:
                if is_speech_energy(_f, vad, energy_threshold):
                    speech_frames += 1
                    silence_frames = 0
                else:
                    silence_frames += 1
            total_frames = len(frames)
            done = ((speech_frames >= min_speech and silence_frames >= max_silence)
                    or (speech_frames == 0 and total_frames >= max_no_speech))
            while not done and not stopping() and total_frames < max_frames:
                chunk = tap.read(FRAME_BYTES)
                if not chunk or len(chunk) < FRAME_BYTES:
                    _log(f"offline: recorder EOF at frame {total_frames}")
                    # Same classification as the sender loop (#57): a quiet
                    # pipe is not proof of a lost mic, poll() is, and the
                    # token is the only signal that says we asked for it.
                    if recorder_dead is not None and not cancel_token.cancelled:
                        try:
                            proc.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            pass
                        if proc.poll() is not None:
                            _log("offline: recorder exited -- microphone lost")
                            recorder_dead.set()
                    break
                frames.append(chunk)
                total_frames += 1
                is_speech = is_speech_energy(chunk, vad, energy_threshold)
                if is_speech:
                    speech_frames += 1
                    silence_frames = 0
                else:
                    silence_frames += 1
                if total_frames % 3 == 0:
                    GLib.idle_add(self._emit_audio_level,
                                  min(rms_energy(chunk) / 10000.0, 1.0))
                    GLib.idle_add(self._emit_stt_status, is_speech,
                                  min(silence_frames / max_silence if speech_frames
                                      else total_frames / max_no_speech, 1.0))
                if speech_frames >= min_speech and silence_frames >= max_silence:
                    break
                if speech_frames == 0 and total_frames >= max_no_speech:
                    break
            _log(f"offline: REC END speech={speech_frames} total={total_frames}")
        except Exception as exc:
            # Keep whatever was already captured: a broken capture loop is no
            # reason to throw the user's words away as well.
            log.exception("Offline STT capture failed: %s", exc)
            GLib.idle_add(self._emit_error, f"STT failed: {exc}")
        finally:
            tap.close()

        text = ""
        if frames and not cancel_token.cancelled:
            self._set_state("processing")
            text = _rest_stt_fallback(frames, _log) or ""
        self._deliver_stt_result({"text": text}, "offline", cancel_token)

        # Report AFTER delivery, and at most one toast. A lost mic is the
        # actionable diagnosis and outranks "the websocket failed"; text that
        # actually landed outranks both (#57: in addition to the words, never
        # instead of them).
        dead = recorder_dead is not None and recorder_dead.is_set()
        if dead and not cancel_token.cancelled:
            self._report_recorder_dead(0)
        elif text:
            log.warning("Azure STT WebSocket unreachable (%s) — "
                        "recognized offline instead", reason)
        elif reason is not None and not cancel_token.cancelled:
            GLib.idle_add(self._emit_error, f"STT WebSocket failed: {reason}")

    def _streaming_stt_worker(self, cancel_token=None):
        """Thread entry: run one streaming STT session and always retire its
        cancel token, whichever of the cycle body's exits is taken."""
        if cancel_token is None:
            cancel_token = self._cancels.issue("stt-stream")
        try:
            self._streaming_stt_cycle(cancel_token)
        finally:
            self._cancels.retire(cancel_token)

    def _streaming_stt_cycle(self, cancel_token):
        """Streaming STT using speech-to-cli building blocks.

        cancel_token separates the two things _stop_event was being asked to
        mean at once. stop_listening() ends the utterance and KEEPS the text
        (the dictation hotkey); stop() -- D-Bus Stop, POST /stop, "cast stop",
        every user-speech preemption -- ABANDONS it. Both set _stop_event, and
        _stop_event is also cleared by the next start_listening(), so it can
        neither express the difference nor survive a restart. Only stop()
        cancels the token, and only the token gates the transcript.

        In continuous dictation (loop) mode, keeps the recorder process AND
        WebSocket session alive across multiple utterances — only the sender
        thread and per-cycle state are reset between cycles.  This eliminates
        WS session reinit (~50ms), recorder startup, and thread-creation
        overhead that the old start_listening(quick=True) path incurred.
        """
        if not self._cancels.begin(cancel_token):
            log.info("Streaming STT cancelled before it began")
            GLib.idle_add(self._emit_transcription_ready, "")
            self._idle_after_stt()
            return

        def _stopping():
            """This cycle must wind down. Says nothing about keeping the text."""
            return self._stop_event.is_set() or cancel_token.cancelled

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
        # Re-read each cycle (see loop bottom): captured once, a Loop toggle
        # mid-session silently did nothing until the NEXT session — which
        # reads as "the loop is broken" to anyone flipping the pill or
        # casting the spell while the mic is open.
        is_loop = CONFIG.get("continuous_dictation", False)

        # 1. Get prewarmed recorder (or start fresh) — reused across all
        #    cycles in loop mode.
        proc = _take_prewarmed_rec()
        _stale_rc = proc.poll() if proc is not None else None
        if _stale_rc is not None:
            # The prewarm hands over whatever it started and never polls it
            # again; pw-record exits immediately when the (on-demand USB) mic
            # is absent.  Without this the session opens on a corpse and the
            # failure below is diagnosed against a recorder that died minutes
            # ago, in a different device state.
            _log(f"prewarmed recorder already exited (rc={_stale_rc}), discarding")
            proc = None
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

        # Drain the recorder into memory from right here -- before the WS
        # connect, not after it. A connect attempt can hold this thread for
        # 10 s and the pipe only holds 2.048 s, so anything spoken after that
        # used to be destroyed at the source while the connect was still
        # pending. See _RecorderTap.
        tap = _RecorderTap(proc)

        # Everything from here to the end of the cycle loop holds the
        # recorder.  Any raise past this point -- the WS, the REST fallback,
        # the spellbook, the injector, the LLM worker -- used to skip the
        # teardown below: pw-record leaked, the badge stuck on
        # listening/processing, and start_listening() answered busy until a
        # panic stop.  Same envelope as _batch_stt_worker.
        cycle = 0
        user_text = ""       # set each cycle; needed in cleanup for conversation_mode check
        natural_end = False  # set each cycle; needed in cleanup for single-shot restart
        # The recorder died under us -- USB mic yanked, pw-record gone.  A
        # property of the SESSION, not of a cycle, and bound HERE beside the
        # other two for the same reason: the WS-init failure `break` below
        # leaves the cycle loop before any per-cycle state exists, and the
        # cleanup after it reads this flag.
        recorder_dead = threading.Event()
        failed = None
        inj = None           # pinned backend; bound below, released in cleanup
        try:
            # 2. Get persistent WebSocket (with exponential backoff retry).
            ws = None
            ws_fresh = False
            # With a Wyoming fallback configured, one failed connect trips
            # the breaker (the batch paths' one-strike rule) instead of
            # spending 1+2+4 s of backoff on a wire already known to be down.
            # The tap keeps recording throughout either way.
            _ws_max_attempts = 1 if wyoming_mod.enabled() else 4
            _ws_backoff = 1.0  # seconds, doubles each attempt, caps at 30s
            for attempt in range(_ws_max_attempts):
                try:
                    ws, ws_fresh = _get_stt_ws()
                    break
                except Exception as exc:
                    _log(f"WS connect attempt {attempt + 1}/{_ws_max_attempts} failed: {exc}")
                    _invalidate_stt_ws()
                    if attempt == _ws_max_attempts - 1:
                        # Azure is unreachable: trip the breaker so the next
                        # presses skip the WS entirely, and finish THIS
                        # session offline instead of dropping words that are
                        # already recorded. It owns its own reporting --
                        # delivery first, then at most one toast.
                        wyoming_mod.mark_azure_down()
                        self._offline_stt_session(
                            proc, tap, cancel_token, _stopping, _log,
                            reason=exc, recorder_dead=recorder_dead)
                        return
                    # Exponential backoff before next attempt
                    delay = min(_ws_backoff, 30.0)
                    _log(f"WS retry in {delay:.1f}s")
                    time.sleep(delay)
                    _ws_backoff *= 2

            # --- Mode flags (stable across cycles) ---
            # Pin the injection backend. Everything this cycle puts in front
            # of the cursor goes through `inj`, never through a fresh
            # get_injector() -- see the re-pin at the top of the loop.
            inj = get_injector()
            live_typing = (CONFIG.get("dictation_mode", True)
                           and not CONFIG.get("conversation_mode", False)
                           and inj.available())
            # Wake gate (spec §4.3): ONE verdict per session, taken before the
            # first partial is typed. Gating only the final paste left
            # live-typed partials and the Keep-Live-Text path
            # (skip_final_paste, default on) ungated, so in the default config
            # the gate never fired (#55).  Scoped to the dictation branches:
            # conversation mode types nothing at the cursor, so it must never
            # even ask -- an unknown field is not its business.
            wake_blocked = (CONFIG.get("dictation_mode", True)
                            and not CONFIG.get("conversation_mode", False)
                            and self._wake_gate_blocks())
            if wake_blocked:
                live_typing = False
            use_lexical = CONFIG.get("terminal_mode", False)

            # ---------------------------------------------------------------
            # Main cycle loop — runs once in single-shot mode, loops in
            # continuous dictation mode.  Recorder and WS stay alive.
            # ---------------------------------------------------------------
            while True:
                cycle += 1
                if cycle > 1:
                    # Audio recorded while the PREVIOUS utterance was being
                    # processed (and its reply spoken) is not part of this
                    # one. Trim to what the kernel pipe used to hold on its
                    # own, so loop turnaround keeps its old behaviour instead
                    # of feeding the recognizer the service's own TTS.
                    dropped = tap.trim(_PIPE_FRAMES)
                    if dropped:
                        _log(f"trimmed {dropped} inter-cycle frames")
                # Pick up a mid-session Loop toggle in BOTH directions.
                is_loop = CONFIG.get("continuous_dictation", False)

                # Re-pin between utterances -- and ONLY between utterances.
                # "cast typing engine" flips CONFIG['injection_method'] on the
                # spell thread; get_injector() would then hand the NEW backend
                # the job of retracting text the OLD one typed (ibus->ydotool
                # sends real Backspaces into the user's document, ydotool->ibus
                # leaves the incantation on screen -- #46).
                #
                # The rebuild only cancel()s the outgoing backend, and every
                # text path re-acquires (_ensure_session -> acquire), so the
                # backend being dropped has to be handed back HERE or it sits
                # on the user's input method until SESSION_MAX_SECONDS. end()
                # is idempotent (injector.py), and gating on an actual identity
                # change keeps the 0.4 s FOCUS_WAIT re-acquire off the common
                # no-swap path.
                nxt = get_injector()
                if nxt is not inj:
                    # getattr, not attribute access: `name` has a default on
                    # the Injector base precisely so a partial backend
                    # degrades instead of crashing (injector.py). Taking the
                    # whole STT cycle down for a LOG LINE is the worst
                    # possible trade, and this line sits on a path whose only
                    # job is to hand a backend back cleanly.
                    _log(f"injection backend swapped mid-session: "
                         f"{getattr(inj, 'name', '?')} -> "
                         f"{getattr(nxt, 'name', '?')}")
                    try:
                        inj.end()
                    except Exception:
                        log.debug("Outgoing injector end() failed", exc_info=True)
                    inj = nxt

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
                typer = _LiveTyper(typed_partial, _log, inj) if live_typing else None

                # 5. Sender thread: calibrate noise, send audio with VAD.
                #    A new sender thread is created each cycle, but the same proc
                #    (recorder) feeds it.  The sender does NOT terminate proc —
                #    that is handled by the outer cleanup below.
                def send_audio(_req_id=request_id, _raw_frames=raw_frames,
                               _end_word_event=end_word_event,
                               _sender_done=sender_done,
                               _rec_dead=recorder_dead):
                    try:
                        # Calibrate noise threshold (cached — reads only 1 frame after first call)
                        energy_threshold, cal_frames = calibrate_noise(tap)
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

                        while not _stopping():
                            chunk = tap.read(FRAME_BYTES)
                            if not chunk or len(chunk) < FRAME_BYTES:
                                _log(f"recorder EOF at frame {total_frames}")
                                # A short read on this pipe is EOF, and
                                # pw-record closes stdout only when it exits --
                                # but "the pipe went quiet" is not by itself
                                # proof of a lost mic, so poll() is the
                                # positive control for the claim.
                                #
                                # cancel_token is the only signal that says WE
                                # asked for this death: stop() cancels every
                                # live token BEFORE it kills the procs.
                                # _stop_event cannot serve -- stop_listening()
                                # sets it, and so does turn.end in single-shot
                                # mode, which is exactly how the report ends up
                                # masked by the session's own natural end.
                                if not cancel_token.cancelled:
                                    try:
                                        proc.wait(timeout=0.5)
                                    except subprocess.TimeoutExpired:
                                        pass
                                    _rc = proc.poll()
                                    if _rc is not None:
                                        _log(f"recorder exited rc={_rc}"
                                             f" -- microphone lost")
                                        _rec_dead.set()
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

                try:
                    while time.time() < deadline and not _stopping():
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
                                if typer is not None:
                                    # Terminal mode: smart lowercase (preserves filesystem casing)
                                    raw = _terminal_lowercase(raw_partial[0]) if use_lexical else raw_partial[0]
                                    # Off-thread: ydotool would otherwise hold this
                                    # loop for 12 ms/char while hypotheses pile up.
                                    typer.submit(raw)
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
                finally:
                    # Everything below reads typed_partial synchronously, so the
                    # worker must be idle before the final-commit arithmetic.
                    if typer is not None:
                        typer.close()

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

                # The stop that means "abandon". Everything downstream of here
                # puts text somewhere the user can see it -- the REST fallback,
                # the spellbook, the LLM, the cursor -- so this is the one gate
                # that has to hold. _stop_event cannot serve: stop_listening()
                # sets it too, and it wants the text typed.
                if cancel_token.cancelled:
                    _log(f"cancelled — discarding transcript ({len(user_text)} chars)")
                    if live_typing and typed_partial[0]:
                        inj.send_backspaces(len(typed_partial[0]))
                        typed_partial[0] = ""
                    GLib.idle_add(self._emit_transcription_ready, "")
                    user_text = ""
                    break

                # A recorder can also die without the sender seeing EOF: the
                # cycle may have ended on the end word or the silence timeout
                # first.  Latch it HERE, because _reap_recorder() in the
                # cleanup below kills proc -- after that poll() can no longer
                # tell a lost mic from a routine teardown.
                if not recorder_dead.is_set() and not cancel_token.cancelled:
                    _rc = proc.poll()
                    if _rc is not None:
                        _log(f"recorder gone outside EOF (rc={_rc})"
                             f" -- microphone lost")
                        recorder_dead.set()

                if not user_text and raw_frames and not got_phrase:
                    _log(f"WS returned nothing, falling back to REST STT (frames={len(raw_frames)})")
                    user_text = _rest_stt_fallback(raw_frames, _log) or ""
                elif not user_text and got_phrase:
                    _log(f"WS analyzed audio but found no speech (skipping REST fallback)")

                user_text = _strip_end_word(user_text, end_word)
                if use_lexical and user_text:
                    user_text = _terminal_lowercase(user_text)
                    user_text = _terminal_numbers(user_text)
                    user_text = _terminal_symbols(user_text)
                _log(f"FINAL: {repr(user_text[:100])}")

                # Spell incantations ("cast …") short-circuit typing/LLM routing;
                # matched on the raw transcript before punctuation substitution.
                # In loop mode the cycle continues listening; replies queue until
                # the mic closes (never TTS over an open mic).
                if user_text and self._try_cast(user_text):
                    if live_typing and typed_partial[0]:
                        inj.send_backspaces(len(typed_partial[0]))
                    GLib.idle_add(self._emit_transcription_ready, user_text)
                    if (is_loop and not _stopping()
                            and not recorder_dead.is_set()
                            and CONFIG.get("continuous_dictation", False)):
                        # Let the spell's spoken reply play before re-opening the mic
                        self._drain_speech_gap()
                        if _stopping():
                            break
                        self._set_state("listening")
                        continue
                    break

                # 8. Post-process: voice commands and auto-corrections.
                # Terminal text already went through _terminal_symbols — the
                # prose table would mangle it ("dash" -> em-dash).
                if user_text:
                    if not use_lexical:
                        user_text = apply_voice_commands(user_text)
                    user_text = apply_auto_corrections(user_text)

                # 9. Emit results and type/copy
                # In loop mode, skip the "processing" flicker if nothing was said —
                # just silently re-enter listening on the next cycle.
                if (is_loop and not user_text and not _stopping()
                        and not recorder_dead.is_set()):
                    if live_typing and typed_partial[0]:
                        inj.send_backspaces(len(typed_partial[0]))
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
                            inj.send_backspaces(len(typed_partial[0]))
                            time.sleep(0.02)
                        # The pin is handed down so the reply's <type> paste
                        # uses THIS utterance's backend: one utterance, one
                        # backend, the same rule that put the pin into
                        # _LiveTyper (#46/#61).
                        #
                        # ⚠️ This depends on the call being SYNCHRONOUS on the
                        # cycle thread. The cleanup below releases `inj`, and
                        # it only runs after this returns. Make the reply
                        # async and inj.end() lands BEFORE the paste -- the
                        # paste would then go through an ended backend, which
                        # is worse than the fresh get_injector() this replaced.
                        # (Note that every reply repro drives the worker on
                        # its own thread, so none of them would catch it.)
                        self._conversation_worker(user_text, inj)
                        if not CONFIG.get("continuous_dictation", False):
                            self._save_config_flag("conversation_mode", False)
                        # _conversation_worker handles its own restart/warmup
                        break  # exit cycle loop; cleanup below

                    # Type at cursor (dictation mode) or just copy to clipboard
                    if CONFIG.get("dictation_mode", True):
                        if wake_blocked:
                            pass  # refused at session start; nothing was live-typed either
                        elif live_typing and (is_loop or CONFIG.get("skip_final_paste", False)):
                            if is_loop and typed_partial[0]:
                                # Final correction: if Azure's final differs from what
                                # was live-typed, surgically fix the divergent tail.
                                if typed_partial[0] != user_text:
                                    inj.replace_text(typed_partial[0], user_text)
                                # Separator before the next utterance. A pre-edit
                                # backend's coalescer restores its own between
                                # commits; typing one here would double it.
                                if not inj.supports_preedit():
                                    inj.type_text(" ")
                            # Keep the live text: on ydotool it is already really
                            # typed (no-op); on IBus it is still a volatile pre-edit
                            # that end() would DISCARD at idle, so commit it (#45).
                            inj.finalize(user_text)
                        elif live_typing:
                            inj.send_backspaces(len(typed_partial[0]))
                            time.sleep(0.02)
                            inj.paste(user_text)
                        else:
                            inj.commit(user_text)
                    else:
                        if live_typing and typed_partial[0]:
                            inj.send_backspaces(len(typed_partial[0]))
                        clipboard_write(user_text)
                else:
                    log.info("No speech detected")
                    if live_typing and typed_partial[0]:
                        inj.send_backspaces(len(typed_partial[0]))
                    GLib.idle_add(self._emit_transcription_ready, "")

                # 10. Decide whether to loop or exit.  A dead recorder ends
                # the session: there is nothing left to listen with, and the
                # loop would otherwise read every EOF as "no speech" and spin.
                if is_loop and not _stopping() and not recorder_dead.is_set():
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
        except Exception as exc:
            failed = exc
            log.exception("Streaming STT failed: %s", exc)
            _invalidate_stt_ws()  # session state is unknown; reconnect next time
        finally:
            # ---------------------------------------------------------------
            # Cleanup: the recorder was kept alive across cycles.
            # ---------------------------------------------------------------
            tap.close()
            self._reap_recorder(proc)
            # Hand the pinned backend back. Safe here, and only here: every
            # path that delivers text has already run by the time the cycle
            # loop is left -- step 9's finalize/paste/commit, and #57's
            # latched dead-recorder verdict, which delivers through that same
            # path and only then reports. end() FLUSHES the IBus coalescer
            # ("flush what is buffered, then hand the IME back"); cancel() is
            # the one that discards.
            #
            # It cannot be left to the idle hook: that ends whatever
            # get_injector() returns NOW, which after a mid-session swap is a
            # different object from the one this cycle typed through -- and
            # that one would hold the user's input method until
            # SESSION_MAX_SECONDS. Once per session, not per cycle: on the
            # no-swap path this is the same object the idle hook is about to
            # end anyway and end() is idempotent, whereas ending every cycle
            # would force a restore + 0.4 s FOCUS_WAIT re-acquire on each
            # loop utterance.
            if inj is not None:
                try:
                    inj.end()
                except Exception:
                    log.debug("Pinned injector end() failed", exc_info=True)

        # Reported ONCE per session, and only here: after every exit above
        # has already delivered whatever Azure did recognize.  A mic yanked
        # mid-utterance must not cost the user the words that were already
        # transcribed -- the report is additional to the text, never instead
        # of it.  Gated on the token, not on _stopping(): by this point a
        # single-shot turn.end has set _stop_event and would silence it.
        if recorder_dead.is_set() and not cancel_token.cancelled:
            self._report_recorder_dead(cycle)

        if failed is not None:
            # A dead recorder is upstream of most ways this cycle can raise;
            # a second toast about the symptom buries the actionable one.
            if not recorder_dead.is_set():
                GLib.idle_add(self._emit_error, f"STT failed: {failed}")
            self._idle_after_stt()
            _schedule_warmup()
            return

        # If we exited due to conversation_mode, it already set state + scheduled warmup
        if CONFIG.get("conversation_mode", False) and user_text:
            return

        self._idle_after_stt()

        # If stop_event was set by turn_end (natural_end) in single-shot mode,
        # and continuous dictation is on, restart via start_listening (legacy path
        # for non-loop mode, e.g. conversation_mode toggled on mid-session).
        if (not is_loop and not cancel_token.cancelled
                and not recorder_dead.is_set()
                and CONFIG.get("continuous_dictation", False)
                and (natural_end or not self._stop_event.is_set())):
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
                if self._stt_thread is thread and not thread.is_alive():
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
        # Bump first: an item the dispatcher pulled microseconds ago is no
        # longer in the queue, so draining cannot reach it. The generation
        # tells the dispatcher "a drain crossed your dequeue — drop it".
        self._drain_gen += 1
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

    def _hold_user_speech(self):
        """Claim the agent-queue hold for one user-speech path (refcounted).

        Every claim must be matched by exactly one _release_user_speech, or
        the queue stays silent forever / starts narrating too early.
        """
        with self._user_speech_lock:
            self._user_speech_depth += 1
            self._user_speech_active.set()

    def _release_user_speech(self):
        """Drop one claim; the hold lifts only when the last one is gone."""
        with self._user_speech_lock:
            if self._user_speech_depth > 0:
                self._user_speech_depth -= 1
            else:
                log.warning("User-speech hold released more often than held")
            if self._user_speech_depth == 0:
                self._user_speech_active.clear()

    def _queue_hold_reason(self):
        """Why the dispatcher must not start an item right now (or None).

        Pause is queue-level (unanimous across Web Speech/.NET/Apple): a
        paused service must not start the next item either.
        """
        if self._user_speech_active.is_set():
            return "user speech"
        if state._pause_event.is_set():
            return "paused"
        st = self.current_state
        if st in ("listening", "processing"):
            return st
        return None

    def _tts_dispatcher(self):
        """Daemon thread: plays queued HTTP speech items serially.

        Holds while user speech is active or the mic/LLM is busy. Its own
        'speaking' state between back-to-back items does NOT hold it (the
        idle flap is suppressed via suppress_idle to avoid badge flicker).
        """
        while True:
            # Wait until clear BEFORE dequeuing, so held items stay visible
            # in GET /queue and remain drainable by /stop during user speech.
            while self._queue_hold_reason() is not None:
                time.sleep(0.2)
            gen = self._drain_gen
            try:
                item = self._tts_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            # Issue the cancel token BEFORE publishing the claim, so the token
            # and _queue_current become visible to /skip in the same critical
            # section. Issuing does not touch the wire; a /skip landing between
            # here and playback is remembered by the token and _speak_worker
            # then refuses to start.
            token = self._cancels.issue("queue")
            # The gate above is a check-then-act: this thread parks inside
            # get() with the hold clear, and put_nowait wakes it the instant
            # an item arrives — by which time the mic may be open. Re-check
            # under the claim lock and hand the item back rather than talking
            # over an open mic (or over a /stop that crossed the dequeue).
            with self._queue_current_lock:
                hold = self._queue_hold_reason()
                drained = self._drain_gen != gen
                if hold is None and not drained:
                    self._queue_current = item
                    self._queue_token = token
            if drained:
                self._cancels.retire(token)
                log.info("Speech queue: item %d dropped — drained mid-claim",
                         item.id)
                self._queue_recent.append({"id": item.id,
                                           "outcome": "canceled"})
                continue
            if hold is not None:
                self._cancels.retire(token)
                # Front of the deque, not the tail: FIFO order is the contract.
                with self._tts_queue.mutex:
                    self._tts_queue.queue.appendleft(item)
                    self._tts_queue.not_empty.notify()
                log.debug("Speech queue: item %d held back (%s)",
                          item.id, hold)
                continue
            try:
                if self.current_state != "speaking":
                    self._set_state("speaking")
                # One object is both the playback claim and the cancel verdict.
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
                # The token, not the wire: a worker that started after this
                # one has already lowered the wire, but it cannot rewrite this
                # item's verdict. (_speak_worker returns "interrupted" itself.)
                self._queue_recent.append({"id": item.id,
                                           "outcome": outcome or "done"})
            except Exception:
                log.exception("Speech queue: item %d failed, continuing", item.id)
                self._queue_recent.append({"id": item.id, "outcome": "error"})
            finally:
                # _speak_worker normally retires the token; this covers the
                # paths where it never ran (a raise in _chronicle_append or
                # _set_state) so a token can never leak into the live set.
                self._cancels.retire(token)
                with self._queue_current_lock:
                    self._queue_current = None
                    self._queue_token = None

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
        # The worker's finally releases this; early exits must release here.
        self._hold_user_speech()

        # Stop outside lock to prevent deadlock (stop() acquires multiple locks)
        # drain_queue=False: user preemption drops only the current utterance,
        # the agent backlog survives and resumes after user speech.
        if self.current_state not in ("idle",):
            self.stop(drain_queue=False)

        missing = _speech_ready()
        if missing:
            GLib.idle_add(self._emit_error, missing)
            self._release_user_speech()
            return False

        with self._speak_lock:
            self._set_state("speaking")
            _chronicle_append("spoken", text, voice=voice, source="direct")
            # Issued AFTER the preempting stop() above, or cancel_all() would
            # cancel the very utterance the user just asked for.
            token = self._cancels.issue("speech")
            self._speak_token = token
            self._speak_thread = threading.Thread(
                target=self._speak_worker,
                args=(text.strip(), voice),
                kwargs={"owner_token": token},
                daemon=True,
            )
            try:
                self._speak_thread.start()
            except Exception:
                # Nobody else will run the worker's finally — releasing the
                # hold (and retiring the token) here is the difference between
                # a failed utterance and a permanently silent speech queue.
                self._cancels.retire(token)
                self._release_user_speech()
                raise
            return True

    def _tts_level_cb(self, level):
        """Emit AudioLevel from TTS audio stream for badge VU effect."""
        GLib.idle_add(self._emit_audio_level, level)

    def _run_subtitle_progress(self, text, estimated_duration, stop_event,
                               token):
        """Emit SubtitleUpdate signals every 200ms during TTS playback.

        Runs in a daemon thread alongside TTS. Respects pause and cancel.

        The cancel it respects is its OWN utterance's verdict, never the
        process-global wire (``state._cancel_event``). The wire belongs to
        whichever operation last called ``CancelRegistry.begin()``, so reading
        it here meant two wrong answers: another operation's cancel froze this
        subtitle mid-word, and a later ``begin()`` lowering the wire let a
        cancelled utterance still emit its 100 % "finished" frame. The token is
        set once and never reset, so neither can happen (issue #42).

        Args:
            text: Full text being spoken.
            estimated_duration: Estimated speech duration in seconds.
            stop_event: threading.Event — set when TTS finishes.
            token: CancelToken of the utterance being spoken — this thread's
                only cancellation verdict. None means "no verdict available"
                and is treated as not cancelled.
        """
        start_time = time.monotonic()
        pause_accumulated = 0.0
        pause_start = None
        while not stop_event.is_set():
            if token is not None and token.cancelled:
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

        # Final emission at 100% — only if THIS utterance really finished.
        if token is None or not token.cancelled:
            GLib.idle_add(self._emit_subtitle_update, text, estimated_duration, 100)

    def _subtitle_queue_worker(self, subtitle_q):
        """Single subtitle thread that processes subtitle items from a queue.

        Items are ``(text, estimated_duration, stop_event, token)``; the token
        travels with the sentence so each progress run judges by the reply's
        own verdict rather than the shared wire (issue #42).

        Eliminates per-sentence thread creation overhead (~5-10ms each).
        Reads from subtitle_q until a None sentinel is received.
        """
        while True:
            item = subtitle_q.get()
            if item is None:
                break
            text, estimated_duration, stop_event, token = item
            self._run_subtitle_progress(text, estimated_duration, stop_event,
                                        token)

    def _speak_worker(self, text, voice=None, quality=None, output_file=None,
                      user_initiated=True, suppress_idle=False,
                      owner_token=None):
        """TTS using speech_tts.tts().

        Runs as a background thread for user speech (speak()) and
        synchronously inside the queue dispatcher for HTTP items.
        Returns "done", "error" or "interrupted".

        owner_token is a CancelToken: it is simultaneously the playback claim
        (the _speak_token fence) and this utterance's cancel verdict. Whoever
        issued it hands it over; retiring it is this worker's job, because the
        issuer may have returned long before the audio finishes.
        """
        outcome = "done"
        token = (owner_token if isinstance(owner_token, CancelToken)
                 else self._cancels.issue("speech"))
        started = self._cancels.begin(token)
        try:
            if not started:
                # Cancelled between the claim and the first note: never start,
                # but fall through the finally so the state fence, the hold and
                # the token are all released exactly as after a real playback.
                outcome = "interrupted"
                return outcome
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
                args=(text, est_dur, sub_stop, token),
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
            # Drop the wire first: everything below (warmup, loop restart) runs
            # with no stale cancel pending.
            self._cancels.retire(token)
            if token.cancelled:
                outcome = "interrupted"   # killed mid-play (skip/stop/preempt)
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
                    and self._speak_token is token):
                self._set_state("idle")
            _schedule_warmup()
            if user_initiated:
                self._release_user_speech()

            # Hands-free loop: after TTS finishes in conversation mode,
            # automatically restart listening if continuous_dictation is enabled.
            # user_initiated guard: queued agent chatter must not re-open the mic.
            if (user_initiated
                    and CONFIG.get("continuous_dictation", False)
                    and CONFIG.get("conversation_mode", False)
                    and not self._stop_event.is_set()):
                log.info("Hands-free: auto-restarting listening after TTS")
                GLib.idle_add(self._restart_listening_cb(
                    lambda: (CONFIG.get("continuous_dictation", False)
                             and CONFIG.get("conversation_mode", False)
                             and not self._stop_event.is_set())))
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
        out from under the cancel. The registry never calls back into the
        service (its lock is innermost) and cancel_active() only sets an event
        and signals tracked subprocesses, so there is no deadlock path.

        Cancelling the item's own token — not just the shared wire — is what
        makes the skip stick: the item records "interrupted" even if a later
        worker takes the wire down first, and an item cancelled between the
        claim and its first note never starts at all.
        """
        with self._queue_current_lock:
            current = self._queue_current
            if current is None:
                return None
            if item_id is not None and current.id != item_id:
                return None
            self._cancels.cancel(self._queue_token)
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

    def _wake_gate_blocks(self):
        """spec §4.3 (opt-in, wake_word_secure_gate): a wake-word session types
        only into a focused field the app has NOT declared password/PIN.

        A deliberate hotkey press is the user vouching for the target; a wake
        word is not. Only the IBus backend sees a purpose at all (ydotool never
        can), so the gate needs injection_method ibus/auto.

        What this does NOT do — measured, not assumed:

        * ibus-daemon (1.5.34) forwards SetContentType to a freshly created
          engine unconditionally, so after acquire()'s FOCUS_WAIT +
          CONTENT_TYPE_GRACE a focused engine has ALWAYS seen a content-type.
          purpose_known() therefore reduces to "focused and not PASSWORD/PIN".
        * A client that never declares a purpose arrives as (0, 0) — bit-for-bit
          identical to a declared FREE_FORM. So the gate FAILS OPEN on an
          undeclared field and CANNOT detect an undeclared password box.
        * It is not X11-only. On GNOME 50 native Wayland the purpose does
          arrive (journal 2026-09-01T07:39:11: "IBus content type purpose=10
          hints=0", wayland session). Any claim that this refuses all
          hands-free typing on Wayland is false.

        Default is OFF.
        """
        if not getattr(self, "_wake_initiated", False):
            return False
        if not CONFIG.get("wake_word_secure_gate", False):
            return False
        inj = get_injector()
        inj.acquire()  # idempotent; lets content-type arrive before we ask
        if inj.purpose_known():
            return False
        # A refused session must not leave the IME swapped in for nothing;
        # idle's end() would also do this, but a loop session never idles.
        try:
            inj.end()
        except Exception:
            log.debug("Injector end() after gate refusal failed", exc_info=True)
        log.info("Wake-word gate: field purpose unknown — not typing")
        self._spell_speak("Unknown field — press the hotkey to dictate here.")
        return True

    def _toggle_config_flag(self, key, default=False):
        """Flip a boolean mode flag, persist it, and return the NEW value.

        Only the flip-and-persist is shared. What stays at each call site:
        the spoken reply (some are one template, some are two distinct
        sentences), and subtitles_toggle's extra GSettings dual-write.

        `default` is a PARAMETER, not a constant: live_subtitles and
        chronicle default to True and the rest to False, so a wrong default
        here would silently invert which way a spell toggles on first use.

        Always goes through _save_config_flag, never `CONFIG[key] = ...`:
        that is the seam keeping the runtime dict and config.json from
        drifting (CLAUDE.md, config dual-write).
        """
        new = not CONFIG.get(key, default)
        self._save_config_flag(key, new)
        return new

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
            new = self._toggle_config_flag("read_notifications")
            return "The notification herald is %s." % ("on" if new else "off")
        elif op == "injection_toggle":
            # The spell is the voice-recoverable way out of an IBus trial, so
            # "auto" (which may already be on IBus) must leave to ydotool, not
            # re-request ibus and change nothing (#56). Unknown values are
            # ydotool in _make_injector(), so they toggle to ibus.
            cur = str(CONFIG.get("injection_method") or "ydotool").strip().lower()
            new = "ydotool" if cur in ("ibus", "auto") else "ibus"
            self._save_config_flag("injection_method", new)
            # Report what was actually built, not what was asked for: an
            # "ibus" request falls back to ydotool when IBus is unreachable.
            if get_injector().name == "ibus":
                return ("Typing through the input method — no key can stick. "
                        "Say the same words to go back.")
            if new == "ibus":
                return ("The input method is unreachable — still typing "
                        "through the virtual keyboard.")
            return "Typing through the virtual keyboard."
        elif op == "press_enter":
            # Hands-free Enter. Deliberately a SPELL ("cast run it") and not
            # a bare voice-command word: the "cast" prefix + pattern match
            # means a misheard mid-sentence word can never execute a command.
            # Silent on success — the command's own output is the feedback.
            # NB for the IBus backend: this is a KEYSTROKE, not a text
            # commit. IBus commit_text("\n") inserts a newline character
            # into the field; it does not press Return, so a shell never
            # runs the command. This site must stay on a key-event backend
            # (spec 5.4, "non-text targets") even after IBus lands.
            get_injector().press_enter()
            return None
        elif op == "loop_toggle":
            new = self._toggle_config_flag("continuous_dictation")
            if new:
                return ("The loop is woven — I will keep listening "
                        "after each phrase.")
            return "The loop is broken — one phrase at a time."
        elif op == "wake_word_toggle":
            new = self._toggle_config_flag("wake_word")
            return "The waking watch is %s." % ("on" if new else "off")
        elif op == "thinking_toggle":
            new = self._toggle_config_flag("llm_thinking")
            if new:
                return ("Deep thought engaged — replies will be slow "
                        "and thorough.")
            return "Deep thought off — fast replies."
        elif op == "subtitles_toggle":
            new = self._toggle_config_flag("live_subtitles", default=True)
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
            new = self._toggle_config_flag("chronicle", default=True)
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
            recorder_eof = False
            try:
                proc = subprocess.Popen(_build_rec_cmd(),
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)

                def _chunks():
                    nonlocal recorder_eof
                    while (CONFIG.get("wake_word", False)
                           and self.current_state == "idle"):
                        data = proc.stdout.read(3200)  # ~100 ms @ 16 kHz s16
                        if not data:
                            recorder_eof = True
                            return
                        yield data

                name = wyoming_mod.detect_stream(host, port, model, _chunks())
                if name and self.current_state == "idle":
                    log.info("Wake word detected (%s) — opening mic", name)
                    GLib.idle_add(lambda: (self.start_listening(quick=True,
                                                                wake=True),
                                           False)[-1])
                    time.sleep(2.0)  # cooldown; state flips to listening anyway
                elif recorder_eof:
                    # Success-shaped failure: pw-record exited or yielded no
                    # audio (mic unplugged, stale mic_source). Without a sleep
                    # this loop respawns the recorder and reconnects to the
                    # wake server as fast as it can (#41).
                    now = time.time()
                    if now - last_fail_log > 300:
                        log.warning("Wake watcher: recorder produced no audio "
                                    "(rc=%s) (retrying every 10s)", proc.poll())
                        last_fail_log = now
                    time.sleep(10)
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
        self._hold_user_speech()
        try:
            with self._talk_lock:
                if self.current_state not in ("idle",):
                    self.stop(drain_queue=False)

                missing = _speech_ready(need_azure=True)
                if missing:
                    GLib.idle_add(self._emit_error, missing)
                    return "error: speech not configured"

                self._stop_event.clear()
                self._set_state("speaking")
                # Claim playback ownership so a preempted queue worker's
                # cleanup can't reset our speaking state. The same object is
                # this exchange's cancel verdict.
                token = self._cancels.issue("talk")
                self._speak_token = token

                # Use an event to pass the result back from the worker thread
                result_holder = {"reply": ""}
                done_event = threading.Event()

                self._talk_thread = threading.Thread(
                    target=self._talk_worker,
                    args=(text.strip(), result_holder, done_event, token),
                    daemon=True,
                )
                self._talk_thread.start()

                # Wait for the worker to complete (blocks the D-Bus call)
                done_event.wait()
                return result_holder["reply"]
        finally:
            self._release_user_speech()

    def _talk_worker(self, text, result_holder, done_event, token=None):
        """Background thread: full-duplex TTS+STT via speech_tts.talk_fullduplex()."""
        if token is None:
            token = self._cancels.issue("talk")
        started = self._cancels.begin(token)
        try:
            if not started:
                result_holder["reply"] = ""
                return
            # Show text being spoken as live subtitle on badge
            GLib.idle_add(self._emit_partial_transcription, text)

            # Start subtitle progress thread for progressive reveal during TTS
            speed_factor = 22.0 if self._voice_quality == "fast" else 15.0
            est_dur = max(1.0, len(text) / speed_factor)
            sub_stop = threading.Event()
            sub_thread = threading.Thread(
                target=self._run_subtitle_progress,
                args=(text, est_dur, sub_stop, token),
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
            elif result.get("cancelled") or token.cancelled:
                # token.cancelled outranks the library's verdict: a worker that
                # started after this one may already have taken the wire down.
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
            self._cancels.retire(token)
            self._set_state("idle")
            _schedule_warmup()
            done_event.set()

    def set_language(self, language):
        """Change the STT language at runtime.

        Persist it: "language" is in _SYNC_FLAGS, so a bare CONFIG write is
        overwritten from disk by the next start_listening()'s
        _reload_config_flags() (#133).
        """
        self._save_config_flag("language", language)
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
            # Same authority as a stop: the in-flight utterance is being
            # abandoned, so its transcript must not surface after the restart.
            self._cancels.cancel_all()
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
        # Its own timeout (1 s, not 5), its own success test (an exit-0 but
        # EMPTY read means "try the next tool" here), its own sentinel (None,
        # not ""), and its own trimming. Only the loop is shared.
        for result in _run_first_available(_CLIPBOARD_CMDS, 1, "Clipboard"):
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()[:200]
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

    def _restart_listening_cb(self, still_wanted):
        """A one-shot GLib source callback that restarts listening.

        Collapses `lambda: (self.start_listening(quick=True), False)[-1] if
        <guard> else False`, which appeared four times. The tuple-index trick
        exists only to force the False that makes a GLib source fire once, and
        it is easy to get subtly wrong -- that is what is worth having in one
        place.

        What deliberately STAYS at each call site:
          * the guard, because all four differ (loop only; loop AND
            conversation; stop-event only), and
          * the scheduler, because `idle_add` and `timeout_add(2000)` are a
            timing decision, not boilerplate.

        `still_wanted` is re-checked HERE, when the source fires, not when it
        was scheduled: between the two the user can stop or toggle the loop
        off, and the restart must not happen then. That re-check is why the
        guard is passed as a callable.
        """
        def _cb():
            if still_wanted():
                self.start_listening(quick=True)
            return False
        return _cb

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
            GLib.idle_add(self._restart_listening_cb(
                lambda: not self._stop_event.is_set()))
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

    def _stream_conversation_worker(self, user_text, inj=None):
        """Stream LLM response and start TTS on each complete sentence.

        Uses the unified llm_stream library for all providers (including bedrock).

        `inj` is the caller's PINNED backend when there is one. The streaming
        cycle calls this synchronously from inside its pinned scope (the line
        above the call is `inj.send_backspaces(...)`), so the `<type>` paste
        below belongs to that utterance and must use that utterance's backend
        -- the same rule that put the pin into _LiveTyper (#46/#61). Callers
        with no pin (the batch/offline `_deliver_stt_result`) pass nothing and
        get the current backend, which is correct for them.
        """
        # AI replies are user-initiated speech: hold the agent speech queue
        # for the whole turn (LLM streaming + sentence TTS).
        self._hold_user_speech()
        # Issued at the first spoken sentence, exactly where the old code
        # cleared the wire — before that point a stop is carried by
        # _stop_event and the LLM stream, not by the TTS wire.
        cancel_token = None
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
            aborted = False     # begin() refused: a stop beat the first note

            def _claim_playback():
                """Issue this reply's token and take the wire.

                False means begin() refused because a stop landed between the
                claim and the first note. Every other path in the service
                treats that as "never even start" (_speak_worker returns
                "interrupted" without playing, _streaming_stt_cycle returns
                before opening the mic); this one used to ignore the answer and
                speak anyway, so a reply the user had already stopped was
                spoken in full (#79).
                """
                nonlocal cancel_token, first_sentence
                # One object claims playback AND carries the verdict.
                cancel_token = self._cancels.issue("ai-reply")
                self._speak_token = cancel_token
                if not self._cancels.begin(cancel_token):
                    log.info("AI reply cancelled before its first note — "
                             "nothing spoken")
                    return False
                # Only now: announcing "speaking" for a reply that will never
                # be spoken puts the badge in the same disagreement the token
                # exists to prevent. The queue hold does not depend on this —
                # _hold_user_speech() has held since the turn began.
                self._set_state("speaking")
                # On headphones, prewarm recorder during TTS
                if not CONFIG.get("half_duplex", False):
                    _schedule_warmup()
                first_sentence = False
                return True

            # Single subtitle thread for the entire conversation (Fix 7).
            # cancel_token (issued at the first spoken sentence) rides the
            # queue with every sentence, so a subtitle run judges by this
            # reply's verdict and not the shared wire (issue #42). It is None
            # until then, which _run_subtitle_progress reads as "not
            # cancelled" — the same answer the wire gave before it is issued.
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

                    if first_sentence and not _claim_playback():
                        aborted = True
                        break

                    spoke_anything = True
                    log.info("Streaming TTS sentence: %s", sentence[:80])
                    GLib.idle_add(self._emit_partial_transcription, sentence)
                    _sf = 22.0 if self._voice_quality == "fast" else 15.0
                    _sd = max(1.0, len(sentence) / _sf)
                    _ss = threading.Event()
                    subtitle_q.put((sentence, _sd, _ss, cancel_token))
                    speech_tts.tts(sentence, quality=self._voice_quality,
                                   speed=CONFIG.get("speed", 1.0),
                                   pitch=CONFIG.get("pitch", "default"),
                                   volume=CONFIG.get("volume", "default"),
                                   audio_level_cb=self._tts_level_cb)
                    _ss.set()

                    if self._stop_event.is_set():
                        break

                if aborted:
                    break

            # Speak any remaining buffered text after stream ends
            remainder = buffer.strip()
            # Decided once: _stop_event could otherwise flip between a guard
            # that claims playback and a guard that speaks.
            speak_remainder = (bool(remainder) and not aborted
                               and not self._stop_event.is_set())
            if speak_remainder and first_sentence and not _claim_playback():
                aborted = True
                speak_remainder = False
            if speak_remainder:
                spoke_anything = True
                log.info("Streaming TTS remainder: %s", remainder[:80])
                GLib.idle_add(self._emit_partial_transcription, remainder)
                _sf = 22.0 if self._voice_quality == "fast" else 15.0
                _sd = max(1.0, len(remainder) / _sf)
                _ss = threading.Event()
                subtitle_q.put((remainder, _sd, _ss, cancel_token))
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
                if type_text and not (cancel_token is not None
                                      and cancel_token.cancelled):
                    log.info("Typing %d chars at cursor (from streamed reply)", len(type_text))
                    # The utterance's backend, not whatever is current: a
                    # "cast typing engine" mid-reply rebuilds the process-wide
                    # injector, and this paste belongs to the cycle that is
                    # still holding the old one.
                    (inj or get_injector()).paste(type_text)
                elif type_text:
                    log.info("AI reply cancelled — %d chars NOT typed",
                             len(type_text))

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
                GLib.timeout_add(2000, self._restart_listening_cb(
                    lambda: not self._stop_event.is_set()))
        finally:
            self._cancels.retire(cancel_token)
            self._release_user_speech()

    # -- Conversation worker ------------------------------------------------

    def _conversation_worker(self, user_text, inj=None):
        """Send transcribed text to an LLM and speak the response.

        All providers (including bedrock) now stream via llm_stream.

        `inj`: see _stream_conversation_worker. Optional and TRAILING on
        purpose -- five repros across two suites call this as
        `Thread(target=svc._conversation_worker, args=(text,))`, and a
        required or leading parameter would break every one of them.
        """
        if not stream_chat:
            GLib.idle_add(self._emit_error, "LLM streaming library not available (llm_stream not found)")
            self._set_state("idle")
            return
        return self._stream_conversation_worker(user_text, inj)

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

        # Record the verdict on every live operation, then kill active procs
        # and raise the wire. Verdicts first, and before the joins below: a
        # worker that outlives its 3s join can still tell that it was stopped,
        # which is the difference between "the transcript is discarded" and
        # "the transcript is typed after the user asked for silence".
        cancelled = self._cancels.cancel_all()
        if cancelled:
            log.info("Stop: cancelled %s",
                     ", ".join(repr(t) for t in cancelled))

        # Wait for threads to finish with short timeouts
        # Skip joining the current thread (e.g. conversation mode calls speak() from STT thread)
        #
        # A reference is dropped only once its worker is really gone. A join
        # that timed out (stt_fixed is mid-POST to Azure for up to 30s and
        # never sees the stop) leaves a zombie that still owns the recorder
        # and the shared WebSocket; forgetting it would let the next hotkey
        # spawn a second worker beside it (#40). Keeping the reference makes
        # start_listening() refuse until the zombie exits, and lets a later
        # stop() join it again.
        me = threading.current_thread()

        with self._stt_lock:
            stt_t = self._stt_thread
        if stt_t is not None and stt_t is not me:
            stt_t.join(timeout=3)
            if stt_t.is_alive():
                log.warning("STT thread did not finish in 3s")
        with self._stt_lock:
            if self._stt_thread is stt_t and stt_t is not None and not stt_t.is_alive():
                self._stt_thread = None

        with self._speak_lock:
            speak_t = self._speak_thread
        if speak_t is not None and speak_t is not me:
            speak_t.join(timeout=3)
            if speak_t.is_alive():
                log.warning("Speak thread did not finish in 3s")
        with self._speak_lock:
            if self._speak_thread is speak_t and speak_t is not None and not speak_t.is_alive():
                self._speak_thread = None

        with self._talk_lock:
            talk_t = self._talk_thread
        if talk_t is not None and talk_t is not me:
            talk_t.join(timeout=3)
            if talk_t.is_alive():
                log.warning("Talk thread did not finish in 3s")
        with self._talk_lock:
            if self._talk_thread is talk_t and talk_t is not None and not talk_t.is_alive():
                self._talk_thread = None

        self._set_state("idle")
        return True

    # -- Cleanup -----------------------------------------------------------

    def shutdown(self):
        """Clean up resources on exit."""
        log.info("Shutting down")
        self.stop()
        get_injector().recover()
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

    # /api/version payload, built once on the first request (see
    # _handle_version).  No TTL: the git facts in it describe the code this
    # process loaded, so they cannot change while it runs.  ThreadingHTTPServer
    # can land two pollers at once, hence the lock.
    _version_cache = None
    _version_cache_lock = threading.Lock()

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
        self._replied = True   # headers go out below; _dispatch must not add a 500
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

    # Every request gets a JSON answer, including the ones that blow up (#128).
    # BaseHTTPRequestHandler lets an exception escape do_GET/do_POST straight
    # into socketserver, which prints a traceback and closes the socket with
    # NO response -- agents saw curl's "Empty reply from server" instead of an
    # error they could parse, and the only trace was a stack dump in the
    # journal. The envelope is the contract: a handler that raises is a bug,
    # but the caller still gets {"ok": false, "error": ...} and a 500.
    _replied = False

    def _dispatch(self, handler):
        self._replied = False   # per request: one handler instance may serve several
        try:
            handler()
        except Exception as exc:  # noqa: BLE001 -- the envelope IS the point
            log.exception("HTTP %s %s failed", self.command, self.path)
            if self._replied:
                return  # headers already on the wire; nothing sane to add
            try:
                self._send_error_json(
                    500, f"internal error: {type(exc).__name__}: {exc}")
            except OSError:
                pass  # client already gone

    def do_GET(self):
        self._dispatch(self._route_get)

    def do_POST(self):
        self._dispatch(self._route_post)

    def _route_get(self):
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

    def _route_post(self):
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
        """Read and parse JSON request body. Returns dict or None on error.

        Every handler does `body.get(...)` on the result, so a body that parses
        but is not an object (a bare list, string, number) is a 400 here, not
        an AttributeError three lines later.
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_error_json(400, "Invalid Content-Length header")
            return None
        if content_length == 0:
            return {}
        try:
            raw = self.rfile.read(content_length)
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_error_json(400, f"Invalid JSON: {exc}")
            return None
        if not isinstance(body, dict):
            self._send_error_json(
                400, f"JSON body must be an object, got {type(body).__name__}")
            return None
        return body

    # -- Endpoint handlers -------------------------------------------------

    def _handle_speak(self):
        body = self._read_json_body()
        if body is None:
            return  # error already sent
        text = body.get("text", "")
        if not isinstance(text, str) or not text.strip():
            self._send_error_json(400, "Missing or empty 'text' field")
            return

        # Reject up front rather than enqueueing items doomed to fail —
        # a service that cannot speak must not answer 200 and then say nothing.
        missing = _speech_ready()
        if missing:
            self._send_error_json(503, missing)
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
        if not isinstance(text, str) or not text.strip():
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
        """GET /chronicle?limit=20&q=text&kind=you|spoken — newest last.

        A `q` search reaches back into the cold archive (whole history, this
        handler runs on an HTTP worker thread); without it this is the recent
        list and stays a bounded read of the hot generations. `archive`
        reports which one you got, so a caller can tell "not in the last few
        thousand lines" from "not in the Chronicle at all".
        """
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
        q = params.get("q", [None])[0]
        entries = _chronicle_read(limit=limit, q=q, kind=kind,
                                  include_archive=bool(q))
        self._send_json({"entries": entries, "count": len(entries),
                         "enabled": CONFIG.get("chronicle", True),
                         "archive": bool(q)})

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
        minimal payload when realm-sigil isn't installed).

        The payload is built once and reused.  hash/branch/dirty describe the
        code this process loaded and cannot change while it runs, so deriving
        them per request forked three git processes — one of them a full
        working-tree scan — on every poll of the status board (#53).  Only
        `uptime` is live, and it is refreshed *in place* so the key order the
        realm-sigil contract ships with is untouched.
        """
        cls = SpeechHTTPHandler
        with cls._version_cache_lock:
            if cls._version_cache is None:
                cls._version_cache = self._build_version_payload()
            payload = dict(cls._version_cache)
        payload["uptime"] = int(time.time() - _SERVICE_START_TIME)
        self._send_json(payload)

    @staticmethod
    def _build_version_payload():
        """The once-per-process half of /api/version: three git reads, the
        realm-sigil call, and the host facts."""
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
        return payload

    def _handle_status(self):
        svc = self.service
        current = svc.current_state
        route = speech_route()
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

        result = {
            "state": current, "paused": paused,
            "speech": route,
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

            # Shells out to wpctl (3 s timeout) on every call, pw-dump when the
            # sink changed and pw-cli until the EC probe is cached -- the same
            # probes #50 measured at 3.9 s while PipeWire was still coming up.
            # The extension calls this at proxy init and on every
            # bus-name-appeared, i.e. exactly that window, so it goes to the
            # pool like every other blocking method (#135).
            elif method_name == "GetAudioInfo":
                self._run_async(invocation, "(s)", self.service.get_audio_info)

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

            # Both of these touch the ledger, so they go to the pool like
            # every other blocking method. A tail-read is fast, but the cases
            # it cannot short — a filtered search that matches nothing, a
            # cold-cache read off a spun-down disk — would still stall
            # StateChanged/SubtitleUpdate/AudioLevel for their whole duration.
            # extension.js calls both through the async proxy
            # (GetChronicleRemote/RespeakRemote), so a reply that arrives a
            # beat later is invisible to it.
            elif method_name == "GetChronicle":
                limit = parameters.unpack()[0]
                self._run_async(
                    invocation, "(s)",
                    lambda n: json.dumps(_chronicle_read(limit=n)),
                    limit if limit > 0 else 20)

            elif method_name == "Respeak":
                entry_id = parameters.unpack()[0]
                self._run_async(
                    invocation, "(b)",
                    lambda i: self.service.respeak(i) is not None,
                    entry_id or None)

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
    parser.add_argument(
        "--restore-ime", action="store_true",
        help="Restore a stranded IBus global engine and exit (ExecStopPost hook)",
    )
    args = parser.parse_args()

    # Crash recovery for the IBus backend, BEFORE anything else happens.
    # Deliberately unconditional -- not gated on injection_method: the run that
    # stranded the engine is not the run that has to clean up after it, and by
    # now the user may well have flipped the config back to ydotool precisely
    # BECAUSE their input method is broken. Measured on GNOME 50.1: a crash
    # mid-session leaves NO global engine and the daemon does not auto-revert,
    # so on a desktop where the keymap comes from an IBus engine this is the
    # difference between self-healing and no working keyboard.
    if args.restore_ime:
        # ExecStopPost= path: restore and exit, never start a service.
        restore_prior_engine("service stop")
        return
    restore_prior_engine("service start")

    # Validate config
    missing = _speech_ready()
    if missing:
        log.error(
            "%s. Set AZURE_SPEECH_KEY or configure ~/.config/speech-to-cli/config.json",
            missing,
        )
        # Continue anyway — we will emit Error signals on method calls
    elif not CONFIG.get("key"):
        log.info("No Azure Speech key: running local-first on the Wyoming server")

    log.info(
        "Starting GNOME Speaks service (speech=%s, region=%s, vad=%s, ws=%s, whisper=%s)",
        _SPEECH_ENGINE, CONFIG.get("region"), HAS_VAD, HAS_WS, HAS_WHISPER,
    )
    log.info("Speech route at start: %s", _speech_route_words())

    # Detect typing tool in background to avoid blocking startup with
    # shutil.which() + pidof subprocess calls (~100-200ms).
    def _init_typing():
        get_injector().prepare()
    threading.Thread(target=_init_typing, daemon=True).start()

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
    handler = DBusHandler(service)

    # Detect audio output, auto-enable echo cancellation, and prewarm the
    # recorder in background. These shell out to wpctl/pw-dump/pw-cli (3 s
    # timeout each) and measured 3.9 s at login while PipeWire was still
    # coming up; run synchronously they held back Gio.bus_own_name and the
    # HTTP bind, so systemd (Type=dbus) and the extension saw no service for
    # the whole window (#50). start_listening()/speak() lazily refresh
    # detection via _audio_detected until this finishes, and every helper
    # here is lock-guarded, so a hotkey racing the thread is harmless.
    def _init_audio():
        _refresh_audio_detection()
        dev_type = CONFIG.get("_detected_output", "unknown")
        ec_present = has_echo_cancel()
        if ec_present and dev_type == "headphones":
            CONFIG["enable_echo_cancel"] = True
            log.info("Auto-enabled echo cancellation (headphones + PipeWire EC detected)")
        log.info("Audio output: %s, echo_cancel=%s, half_duplex=%s",
                 dev_type, ec_present, CONFIG.get("half_duplex", False))
        service._audio_detected = True
        # Prewarm recorder so the first listen is instant. Same order as the
        # old synchronous startup: detection and the EC decision first.
        _prewarm_recorder()
    threading.Thread(target=_init_audio, daemon=True).start()

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

    # Apply prefs.js edits to config.json while idle (#124). Without this the
    # only reload was a side effect of the non-quick dictation hotkey.
    service._start_config_watch()

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
