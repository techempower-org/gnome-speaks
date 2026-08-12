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
    no events, no SSML claims.
    """
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
    is the only honest failure signal.
    """
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
