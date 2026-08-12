# Spiel speech provider — gnome-speaks voices for any libspiel client

## Problem

gnome-speaks' voices (LAN Piper, optionally Azure) are private to its own
dictation/AI/spell paths. Spiel (`org.freedesktop.Speech.Provider`) is the
freedesktop standard by which TTS engines offer voices system-wide — to Orca
and any libspiel client. No cloud/neural provider exists in that ecosystem
(issue #6). Contract verified against libspeechprovider's interface XML
(2026-07-30, research in scratch/speech-queue-prior-art/spiel-ubuntu.md):

```
method   Synthesize(in h pipe_fd, in s text, in s voice_id,
                    in d pitch,   in d rate, in b is_ssml, in s language)
property Voices  a(ssstas)  read   # (name, id, output_format, features, [langs])
property Name    s          read
# zero signals; discovery = bus name ending ".Speech.Provider";
# cancellation = client closes the fd; concurrent requests expected;
# client owns playback.
```

## Decisions (JP, 2026-08-12)

1. **Piper by default, Azure opt-in.** A screen reader narrating a desktop
   through metered DragonHD burns money silently. `spiel_expose_azure`
   (default false) gates the Azure roster.
2. **Curated voice list, not the catalog.** The Wyoming Piper server
   advertises its full *downloadable* catalog (hundreds, mostly not
   installed); `spiel_voices` (default `["en_GB-cori-high"]`) lists what we
   actually offer.
3. **Feature honesty:** feature bitfield 0 — no word/sentence events, no
   SSML claims. `is_ssml=true` input gets tags stripped defensively;
   pitch/rate accepted and ignored (advertise nothing, half-implement
   nothing).
4. **Off by default** (`spiel_provider: false`) — the public repo ships
   inert; JP's config enables it.

## Components

### `spiel_provider.py` (new module)

- `build_voices(config) -> list[tuple]` — `(display_name, voice_id,
  output_format, features=0, [langs])` per curated voice. Piper entries:
  id = piper voice name, `output_format =
  "audio/x-raw,format=S16LE,channels=1,rate=22050"`, lang derived from the
  piper name prefix (`en_GB-cori-high` → `en-gb`). Azure entries (opt-in):
  id = Azure ShortName, 24 kHz raw format string.
- `synthesize_to_fd(fd, text, voice_id, config)` — runs in a per-request
  daemon thread (contract expects concurrency; the speech queue is
  deliberately uninvolved — the *client* owns playback):
  - strip SSML tags if present (defensive regex),
  - Piper ids → `wyoming.synthesize(...)` → write PCM to fd in 16 KB chunks;
    if the server's actual rate differs from the advertised one, resample
    via `audioop.ratecv` and log once,
  - Azure ids (only when exposed) → Azure REST `raw-24khz-16bit-mono-pcm`
    → stream chunks to fd,
  - `BrokenPipeError`/`EPIPE` = client cancelled: stop silently (normal
    path, per contract), close fd in `finally`, one structured log line
    per request (voice, bytes, outcome).

### Service integration (`gnome-speaks-service.py`)

- Own a **second** bus name `org.gnome.Speaks.Speech.Provider` (suffix is
  the discovery convention). Register interface
  `org.freedesktop.Speech.Provider` at `/org/gnome/Speaks/Speech/Provider`
  mirroring the existing `on_bus_acquired`/`register_object` pattern, with
  a `get_property` handler for `Name`/`Voices` and a method handler that
  extracts the pipe fd from the invocation's `GUnixFDList`
  (`invocation.get_message().get_unix_fd_list().get(handle)`), duplicates
  it, returns the D-Bus call immediately, and hands the fd to
  `synthesize_to_fd` on a thread.
- Only owned/registered when `CONFIG["spiel_provider"]` is true at startup.

### Activation + install

- `org.gnome.Speaks.Speech.Provider.service.in` D-Bus activation file
  (Exec = service path), templated + installed by install.sh exactly like
  the existing `org.gnome.Speaks.service.in` — lets Spiel clients activate
  the provider on demand.

### Config (speech-to-cli `state.py` whitelist + user file)

`spiel_provider` (false), `spiel_voices` (`["en_GB-cori-high"]`),
`spiel_expose_azure` (false).

## Error handling

Unknown voice_id → write nothing, close fd, log WARNING (the contract has
no error channel — an empty stream is the only honest signal). Wyoming/Azure
failure mid-request → stop writing, close fd, WARNING. Provider failures
never touch the main service paths.

## Validation

1. `gdbus introspect --session --dest org.gnome.Speaks.Speech.Provider` →
   interface + properties present; `Voices` matches config.
2. Python test client: create `os.pipe()`, call Synthesize passing the write
   end via `GUnixFDList`, read from the read end → nonzero PCM; play a
   sample through `aplay` for an audible check.
3. Cancellation: close the read end after the first chunk → writer thread
   exits without a service error; service stays `active`.
4. Concurrency: two overlapping Synthesize calls both complete.
5. `spiel_provider: false` → name not owned (gdbus call fails cleanly).
6. Optional grand finale (JP): point Orca's experimental Spiel backend at it.

## Out of scope

Word/sentence event streaming (feature bits stay 0), SSML rendering,
pitch/rate mapping, exposing the full Piper catalog, speech-queue
integration (deliberately — client-owned playback is Spiel's design).
