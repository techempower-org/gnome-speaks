# Spiel Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** gnome-speaks exports `org.freedesktop.Speech.Provider` so Orca/libspiel clients can synthesize with its Piper (and opt-in Azure) voices.

**Architecture:** New `spiel_provider.py` (voice list + fd-writing synthesis, per-request threads, BrokenPipe = normal cancel). The service owns a second bus name `org.gnome.Speaks.Speech.Provider` and registers the interface with a property handler and a method handler that extracts the pipe fd from the invocation's `GUnixFDList`, returns immediately, and synthesizes on a thread. D-Bus activation file mirrors `org.gnome.Speaks.service.in`. Off unless `spiel_provider: true`.

**Tech Stack:** Python stdlib + existing `wyoming` module + Gio/GLib. Validation: Python Gio test client over a real pipe. Spec: `docs/superpowers/specs/2026-08-12-spiel-provider-design.md`.

**File map:**
- speech-to-cli (direct to main): `state.py` whitelist +3 keys
- gnome-speaks branch `feat/spiel-provider`: create `spiel_provider.py`, `org.gnome.Speaks.Speech.Provider.service.in`; modify `gnome-speaks-service.py`, `install.sh`, `README.md`
- User config (not git): `spiel_provider: true`

---

### Task 1 (speech-to-cli): config whitelist

- [ ] **Step 1:** In `state.py` `load_config()`, after the `llm_thinking` line add:

```python
        # Spiel speech provider (org.freedesktop.Speech.Provider)
        "spiel_provider": cfg.get("spiel_provider", False),
        "spiel_voices": cfg.get("spiel_voices", ["en_GB-cori-high"]),
        "spiel_expose_azure": cfg.get("spiel_expose_azure", False),
```

- [ ] **Step 2:** `py_compile`; commit `feat: spiel provider config keys`; push (direct to main — one-liner default keys, established pattern).

### Task 2: `spiel_provider.py`

- [ ] **Step 1: Write the module**

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Spiel speech provider — gnome-speaks voices for any libspiel client.

Implements the synthesis side of org.freedesktop.Speech.Provider (contract
verified against libspeechprovider's interface XML, 2026-07-30): write raw
PCM to a client-supplied pipe fd. The CLIENT owns playback; cancellation is
the client closing the fd, so BrokenPipeError is the normal cancel path,
not an error. Providers must handle concurrent requests — every request
runs on its own thread and this module keeps no shared mutable state.
"""

import logging
import os
import re

import state
import wyoming

log = logging.getLogger("gnome-speaks")

PIPER_FORMAT = "audio/x-raw,format=S16LE,channels=1,rate=22050"
AZURE_FORMAT = "audio/x-raw,format=S16LE,channels=1,rate=24000"
_SSML_TAGS = re.compile(r"<[^>]+>")


def _piper_lang(voice_id):
    """'en_GB-cori-high' → 'en-gb' (best-effort BCP47 from the piper name)."""
    return voice_id.split("-", 1)[0].replace("_", "-").lower()


def build_voices(cfg):
    """Voices property: list of (name, id, output_format, features, [langs]).

    Curated from config — the Wyoming server advertises its full
    *downloadable* catalog, which is not what we offer. Features stay 0:
    no events, no SSML claims."""
    voices = []
    for vid in cfg.get("spiel_voices") or []:
        voices.append((f"{vid} (Piper)", vid, PIPER_FORMAT, 0,
                       [_piper_lang(vid)]))
    if cfg.get("spiel_expose_azure"):
        for vid in (cfg.get("voice"), cfg.get("fast_voice")):
            if vid:
                voices.append((f"{vid} (Azure, metered)", vid,
                               AZURE_FORMAT, 0, ["en-us"]))
    return voices


def _write_chunks(fd, pcm):
    n = 0
    for i in range(0, len(pcm), 16384):
        os.write(fd, pcm[i:i + 16384])
        n += min(16384, len(pcm) - i)
    return n


def _azure_pcm(text, voice_id, cfg):
    """Raw 24 kHz PCM from Azure — only reachable when spiel_expose_azure."""
    key = cfg.get("tts_key") or cfg.get("key")
    region = cfg.get("tts_region") or cfg.get("region", "westus2")
    safe = (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))
    ssml = ('<speak version="1.0" '
            'xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">'
            f'<voice name="{voice_id}">{safe}</voice></speak>')
    resp = state.get_http_session().post(
        f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
        headers={"Ocp-Apim-Subscription-Key": key,
                 "Content-Type": "application/ssml+xml",
                 "X-Microsoft-OutputFormat": "raw-24khz-16bit-mono-pcm"},
        data=ssml.encode(), timeout=30)
    resp.raise_for_status()
    return resp.content


def synthesize_to_fd(fd, text, voice_id, cfg):
    """Blocking; run on a dedicated thread. Writes PCM to fd, closes it.

    The contract has no error channel — an empty stream (immediate close)
    is the only honest failure signal."""
    outcome, written = "done", 0
    try:
        text = _SSML_TAGS.sub("", text).strip()
        if not text:
            outcome = "empty-text"
            return
        if voice_id in set(cfg.get("spiel_voices") or []):
            host = cfg.get("wyoming_host", "")
            if not host:
                outcome = "no-wyoming-host"
                return
            rate, width, channels, pcm = wyoming.synthesize(
                host, int(cfg.get("wyoming_tts_port", 10200)), text,
                voice=voice_id, timeout=30.0)
            if rate != 22050:
                import audioop
                pcm, _ = audioop.ratecv(pcm, width, channels, rate,
                                        22050, None)
                log.info("Spiel: resampled %s %d→22050", voice_id, rate)
            written = _write_chunks(fd, pcm)
        elif cfg.get("spiel_expose_azure"):
            written = _write_chunks(fd, _azure_pcm(text, voice_id, cfg))
        else:
            outcome = "unknown-voice"
            log.warning("Spiel: unknown voice_id %r", voice_id)
    except BrokenPipeError:
        outcome = "cancelled"  # client closed the pipe — normal
    except wyoming.WyomingError as exc:
        outcome = "wyoming-error"
        log.warning("Spiel synth failed: %s", exc)
    except Exception:
        outcome = "error"
        log.exception("Spiel synth failed")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        log.info("SPIEL | %s | %s | %d bytes", voice_id, outcome, written)
```

- [ ] **Step 2: Sanity (module-level, no D-Bus)** — from the repo dir:

```bash
python3 -c "
import os, threading, spiel_provider, state
cfg = state.CONFIG
voices = spiel_provider.build_voices(cfg)
print('voices:', voices)
assert voices and voices[0][1] == 'en_GB-cori-high'
r, w = os.pipe()
t = threading.Thread(target=spiel_provider.synthesize_to_fd,
                     args=(w, 'The realm endures.', 'en_GB-cori-high', cfg))
t.start()
data = b''
while True:
    chunk = os.read(r, 65536)
    if not chunk: break
    data += chunk
t.join(); os.close(r)
print('pcm bytes:', len(data)); assert len(data) > 20000
# cancellation: close reader immediately
r2, w2 = os.pipe(); os.close(r2)
t2 = threading.Thread(target=spiel_provider.synthesize_to_fd,
                      args=(w2, 'cancel me before I finish speaking', 'en_GB-cori-high', cfg))
t2.start(); t2.join(timeout=30); assert not t2.is_alive()
print('MODULE-OK')"
```
Expected: `MODULE-OK` (journal-free; outcomes logged to stderr).

- [ ] **Step 3:** `py_compile`; commit `feat: spiel_provider module — voices + fd synthesis`.

### Task 3: service integration + activation

- [ ] **Step 1:** In `gnome-speaks-service.py` next to `import spellbook`: `import spiel_provider`. Near the existing `INTROSPECTION_XML` constants add:

```python
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
```

- [ ] **Step 2:** Add module-level handlers above `on_bus_acquired`:

```python
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
```

- [ ] **Step 3:** In `main()`, right after the primary `Gio.bus_own_name(...)` block:

```python
    if CONFIG.get("spiel_provider", False):
        Gio.bus_own_name(
            Gio.BusType.SESSION, SPIEL_BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            _on_spiel_bus_acquired, None,
            lambda conn, name: log.warning("Spiel name lost: %s", name))
```

- [ ] **Step 4:** Create `org.gnome.Speaks.Speech.Provider.service.in`:

```
[D-BUS Service]
Name=org.gnome.Speaks.Speech.Provider
Exec=@SERVICE_EXEC@
```

Check how install.sh templates `org.gnome.Speaks.service.in` (`grep -n "service.in" install.sh`) and add the identical stanza for the new file.

- [ ] **Step 5:** Set `spiel_provider: true` in the user config (python json merge). `py_compile`, restart service, `journalctl` shows "Spiel provider registered".

- [ ] **Step 6: Commit** `feat: export org.freedesktop.Speech.Provider (Spiel) — closes #6`.

### Task 4: validation battery + docs + ship

- [ ] **Step 1: D-Bus-level battery** (real client over a real pipe):

```bash
gdbus introspect --session --dest org.gnome.Speaks.Speech.Provider --object-path /org/gnome/Speaks/Speech/Provider | head -20
python3 - <<'EOF'
import os, gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
# Voices property
v = bus.call_sync("org.gnome.Speaks.Speech.Provider",
    "/org/gnome/Speaks/Speech/Provider",
    "org.freedesktop.DBus.Properties", "Get",
    GLib.Variant("(ss)", ("org.freedesktop.Speech.Provider", "Voices")),
    None, Gio.DBusCallFlags.NONE, -1, None)
print("voices:", v.unpack())
# Synthesize over a pipe
r, w = os.pipe()
fdl = Gio.UnixFDList.new(); h = fdl.append(w)
bus.call_with_unix_fd_list_sync("org.gnome.Speaks.Speech.Provider",
    "/org/gnome/Speaks/Speech/Provider",
    "org.freedesktop.Speech.Provider", "Synthesize",
    GLib.Variant("(hssddbs)", (h, "The realm endures across every desktop.",
                               "en_GB-cori-high", 1.0, 1.0, False, "en-gb")),
    None, Gio.DBusCallFlags.NONE, -1, fdl, None)
os.close(w)
data = b""
while True:
    chunk = os.read(r, 65536)
    if not chunk: break
    data += chunk
os.close(r)
print("pcm bytes:", len(data)); assert len(data) > 20000
open("/tmp/claude-1000/-home-jp-Projects-gnome-speaks/8e6dd04e-7a19-4521-8d0d-c21bf4703b48/scratchpad/spiel.pcm", "wb").write(data)
print("DBUS-OK")
EOF
aplay -q -f S16_LE -r 22050 -c 1 /tmp/claude-1000/-home-jp-Projects-gnome-speaks/8e6dd04e-7a19-4521-8d0d-c21bf4703b48/scratchpad/spiel.pcm   # audible check
```

Concurrency: run the Synthesize block twice in overlapping background shells; both must produce full PCM. Unknown voice: `voice_id="nope"` → 0 bytes, immediate EOF, service stays active. Journal shows `SPIEL |` lines with outcomes.

- [ ] **Step 2:** README: new "Spiel provider" subsection under the HTTP API section (what it is, config keys, off by default, Orca pointer). Leak scan branch. Commit docs.
- [ ] **Step 3:** Push, PR (`feat: Spiel speech provider — closes #6`), merge, main, restart, verify issue #6 auto-closed. Update project catalog line? (already mentions ecosystem — skip). Global CLAUDE.md: one clause on the speech-to-cli bullet is optional — skip (README covers it).

---

## Self-review notes

- Spec coverage: curated voices (T2 build_voices), Piper default + Azure opt-in gate (T2 synthesize routing), fd write/cancel semantics (T2), concurrency threads + no shared state (T2/T3), second bus name + property/method handlers + fd extraction (T3), activation file + install.sh (T3 S4), config keys (T1), off-by-default (T3 S3 gate), validation incl. cancellation/concurrency/unknown-voice (T2 S2 + T4 S1), README (T4). No gaps.
- `state.get_http_session` used by `_azure_pcm` — verify it's importable from state (it is; stt.py imports it from state).
- Type consistency: `synthesize_to_fd(fd, text, voice_id, cfg)` matches T3 thread call; `build_voices(cfg)` matches property handler; tuple shape `(s,s,s,t,as)` matches the Variant signature `a(ssstas)`.
