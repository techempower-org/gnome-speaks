# SPDX-License-Identifier: GPL-3.0-or-later
# GNOME Speaks — IBus text injection backend
# Copyright (C) 2025 JP Hein
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Inject text by being an IBus engine, instead of by faking keystrokes.

Why this exists: a `CommitText` D-Bus call has no key-down/key-up pair, so it
cannot leave a modifier stuck. That failure mode -- a wedged key with no hands
free to clear it -- is what this backend eliminates by construction.

It introduces a different one, and the mitigations for it are not optional.
Measured on GNOME 50.1 (spec 5.5): if this process dies between
`SetGlobalEngine(ours)` and `SetGlobalEngine(prior)`, the global engine is left
**empty** and the daemon does not auto-revert. Not "the wrong input method" --
*no* input method, on a desktop where the keyboard layout is itself delivered
by an IBus engine. So:

  * the prior engine is written to $XDG_RUNTIME_DIR before we ever swap it,
  * `restore_prior_engine()` runs at service start and undoes a previous
    run's crash before anything else happens,
  * the systemd unit runs the same restore from `ExecStopPost=`,
  * and a watchdog force-restores a session that outlives its bound.

Any one of those alone is a coin flip. Together they mean the worst case
self-heals on the next service start, which systemd does automatically.

We build on `gi.repository.IBus` rather than hand-rolling the wire protocol.
libibus then owns the two GVariant shapes that are easy to get silently wrong
(the 4-argument preedit update, and variant-wrapped attribute lists).
"""

import logging
import os
import threading
import time
import weakref

from injector import Injector

log = logging.getLogger("gnome-speaks")

try:
    import gi
    gi.require_version("IBus", "1.0")
    from gi.repository import IBus, GLib, GObject, Gio  # noqa: F401
    HAS_IBUS = True
except (ImportError, ValueError) as _exc:  # pragma: no cover - platform dependent
    IBus = None
    HAS_IBUS = False
    log.debug("IBus bindings unavailable: %s", _exc)


# ── identity ─────────────────────────────────────────────────────────────
COMPONENT_NAME = "org.freedesktop.IBus.GnomeSpeaks"
ENGINE_NAME = "gnome-speaks-stt"
ENGINE_PATH = "/org/freedesktop/IBus/Engine/GnomeSpeaks"

# layout "default" means "do not touch the keymap". Hardcoding "us" here --
# as the Rust implementation this design was researched from does -- silently
# switches a Dvorak or AZERTY user's physical keyboard for the whole session.
ENGINE_LAYOUT = "default"

# ── timings ──────────────────────────────────────────────────────────────
FOCUS_WAIT = 0.4          # how long acquire() waits for the daemon's FocusIn
# ibus-daemon focuses this pseudo-client when NO input context has focus --
# e.g. right after a pointer tap on shell chrome. A commit into it vanishes.
FAKE_CLIENT = "fake"
CONTENT_TYPE_GRACE = 0.05  # ...then for SetContentType to settle behind it
COALESCE_DELAY = 0.12     # back-to-back finals merge into one commit
SESSION_MAX_SECONDS = 120  # watchdog: no session may outlive this

# IBus.InputPurpose values we refuse to type into.
_SECURE_PURPOSES = (8, 9)  # PASSWORD, PIN


# ─────────────────────────────────────────────────────────────────────────
# Crash recovery — deliberately module-level and dependency-free
# ─────────────────────────────────────────────────────────────────────────
# These must work with no IbusInjector instance, no service state, and no
# config: they run at service start (before the backend is even chosen) and
# from ExecStopPost= after the process is gone. Keep them that way.

def _state_dir():
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(base, "gnome-speaks")


def prior_engine_path():
    return os.path.join(_state_dir(), "prior-engine")


def write_prior_engine(name):
    """Record the engine to go back to. Called BEFORE the swap, never after."""
    if not name:
        return False
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        tmp = prior_engine_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(name)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, prior_engine_path())
        return True
    except OSError:
        log.warning("Could not persist prior IBus engine", exc_info=True)
        return False


def clear_prior_engine():
    """Drop the breadcrumb. Only ever after a restore actually succeeded."""
    try:
        os.unlink(prior_engine_path())
    except FileNotFoundError:
        pass
    except OSError:
        log.debug("Could not clear prior-engine file", exc_info=True)


INPUT_SOURCES_SCHEMA = "org.gnome.desktop.input-sources"


class _EngineNameMemo:
    """Remember what the daemon calls an XKB layout, per bus (#136).

    On GNOME `derive_restore_target()` runs on EVERY utterance -- the shell
    owns input sources and leaves the daemon's global engine unset, so the
    "fallback" is the normal path -- and 23 ms of it was `list_engines()`:
    deserialising every EngineDesc the daemon knows (974 on the desk it was
    measured on) to find one name. That answer depends on nothing but the
    daemon's engine list, so it is memoised per (layout, variant) for the
    lifetime of the bus object it was asked of. A different bus -- a
    reconnect, or the probe `restore_prior_engine()` opens at startup --
    starts from nothing, so the dependency-free path is never handed a name
    learned over some other connection.

    Only a FOUND name is stored. A miss is asked again next time, so a layout
    the daemon does not (yet) know is never shadowed by a remembered "no".

    Deliberately NOT cached: the input-sources read that decides WHICH layout
    to look up. It is the authority the shell itself writes, it costs ~0.5 ms,
    and re-reading it per call is what makes a layout switch take effect on
    the very next utterance -- with no `changed` signal to plumb and no main
    loop for that signal to depend on (a GSettings object created on the STT
    worker thread would deliver its signals to a main context nothing
    iterates, and `restore_prior_engine()` runs before the loop exists).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._bus_ref = None      # weakref to the bus these names came from
        self._bus_strong = None   # only for a bus that cannot be weakly referenced
        self._names = {}

    def _same_bus(self, bus):
        if self._bus_ref is not None:
            return self._bus_ref() is bus
        return self._bus_strong is not None and self._bus_strong is bus

    def _bind(self, bus):
        self._names = {}
        self._bus_ref = self._bus_strong = None
        try:
            self._bus_ref = weakref.ref(bus)
        except TypeError:
            self._bus_strong = bus

    def get(self, bus, layout, variant):
        with self._lock:
            if not self._same_bus(bus):
                return None
            return self._names.get((layout, variant))

    def put(self, bus, layout, variant, name):
        if not name:
            return                      # never remember a miss
        with self._lock:
            if not self._same_bus(bus):
                self._bind(bus)
            self._names[(layout, variant)] = name

    def reset(self):
        with self._lock:
            self._names = {}
            self._bus_ref = self._bus_strong = None


_ENGINE_NAMES = _EngineNameMemo()


def _xkb_engine_for(bus, layout, variant):
    """Find the daemon's engine name for an XKB layout/variant pair.

    Engine names look like `xkb:<layout>:<variant>:<lang3>`, and the language
    third is NOT derivable from the layout -- 'de+neo' is `xkb:de:neo:ger`, not
    `...:eng`. So ask the daemon what it actually has rather than constructing
    a name it may not know; setting a nonexistent engine is how you end up
    exactly where this function is trying to rescue you from.

    Asked once per (bus, layout, variant); see `_EngineNameMemo`.
    """
    cached = _ENGINE_NAMES.get(bus, layout, variant)
    if cached:
        return cached
    prefix = "xkb:%s:%s:" % (layout, variant)
    try:
        matches = sorted(e.get_name() for e in bus.list_engines()
                         if e.get_name().startswith(prefix))
    except Exception:
        log.debug("Could not list IBus engines", exc_info=True)
        matches = []
    if not matches:
        return None
    chosen = None
    if len(matches) == 1:
        chosen = matches[0]
    else:
        # Several languages share this layout; prefer the session's own.
        lang = (os.environ.get("LANG") or "")[:2].lower()
        if lang:
            for name in matches:
                if name.rsplit(":", 1)[-1].lower().startswith(lang[:2]):
                    chosen = name
                    break
        if chosen is None:
            for name in matches:
                if name.endswith(":eng"):
                    chosen = name
                    break
        if chosen is None:
            chosen = matches[0]
    _ENGINE_NAMES.put(bus, layout, variant, chosen)
    return chosen


def derive_restore_target(bus=None):
    """Work out what the user's input source *should* be, from GNOME itself.

    Needed because `GetGlobalEngine` frequently fails with "No global engine"
    on GNOME: the shell manages input sources itself and simply leaves the
    daemon's global engine unset. Recording None there and calling it a day is
    how a session gets stranded on our engine after one dictation cycle.

    org.gnome.desktop.input-sources is the authority the shell itself uses:
    mru-sources first (what the user last had), else sources[0].
    """
    if not HAS_IBUS:
        return None
    try:
        settings = Gio.Settings.new(INPUT_SOURCES_SCHEMA)
    except Exception:
        log.debug("No %s schema; cannot derive a restore target",
                  INPUT_SOURCES_SCHEMA, exc_info=True)
        return None
    entries = []
    for key in ("mru-sources", "sources"):
        try:
            entries.extend(settings.get_value(key).unpack() or [])
        except Exception:
            log.debug("Could not read %s", key, exc_info=True)
    if not entries:
        return None
    if bus is None:
        try:
            bus = IBus.Bus()
        except Exception:
            bus = None
    for kind, source_id in entries:
        if kind == "ibus":
            return source_id                      # already an engine name
        if kind != "xkb":
            continue
        layout, _, variant = source_id.partition("+")
        if bus is not None:
            name = _xkb_engine_for(bus, layout, variant)
            if name:
                return name
        # Last resort: the conventional construction. Unverified, but better
        # than leaving the user on our engine.
        guess = "xkb:%s:%s:eng" % (layout, variant)
        log.debug("Falling back to unverified engine name %r", guess)
        return guess
    return None


def read_prior_engine():
    try:
        with open(prior_engine_path(), encoding="utf-8") as fh:
            return fh.read().strip() or None
    except (OSError, ValueError):
        return None


def restore_prior_engine(reason="startup"):
    """Undo a swap this process is no longer around to undo itself.

    Safe to call always: no file means the last run ended cleanly, and that is
    the overwhelmingly common case. Returns True only when a restore actually
    happened, so callers can log the interesting case and stay quiet otherwise.
    """
    name = read_prior_engine()
    if not name and HAS_IBUS:
        # No breadcrumb, but we may still be the installed engine -- a crash
        # before the breadcrumb was written, or a build that predates it.
        # Being stranded is detectable without one, so detect it.
        try:
            IBus.init()
            probe = IBus.Bus()
            if probe.is_connected():
                current = probe.get_global_engine()
                if current is not None and current.get_name() == ENGINE_NAME:
                    name = derive_restore_target(probe)
                    if name:
                        log.warning("Found the session stranded on %s with no "
                                    "breadcrumb; restoring %r", ENGINE_NAME, name)
        except Exception:
            log.debug("Stranded-engine probe failed", exc_info=True)
    if not name:
        return False
    if not HAS_IBUS:
        log.warning("Stranded IBus engine recorded (%s) but IBus bindings are "
                    "missing; cannot restore", name)
        return False
    try:
        IBus.init()
        bus = IBus.Bus()
        if not bus.is_connected():
            log.warning("Stranded IBus engine recorded (%s) but the daemon is "
                        "not reachable; leaving the breadcrumb for next time", name)
            return False
        current = bus.get_global_engine()
        current_name = current.get_name() if current else None
        if current_name == name:
            # Someone already put it back; the breadcrumb is just stale.
            clear_prior_engine()
            return False
        bus.set_global_engine(name)
        clear_prior_engine()
        log.warning("Restored IBus global engine to %r after %s "
                    "(was %r) -- previous run did not shut down cleanly",
                    name, reason, current_name)
        return True
    except Exception:
        log.warning("IBus restore failed", exc_info=True)
        return False


# ─────────────────────────────────────────────────────────────────────────
# Commit coalescing
# ─────────────────────────────────────────────────────────────────────────

class _Coalescer:
    """Merge back-to-back finals into a single commit.

    Rapid successive commits race in the daemon and only the last one lands in
    the target -- the "only the last second or two appears" bug. Loop mode
    fires finals back-to-back, which is exactly that shape.

    The whitespace rule inserts a separator only when neither side already
    carries one, so a backend emitting stripped segments gets spaces restored
    and one emitting natural leading spaces never gets doubles.
    """

    def __init__(self):
        self.pending = ""
        self.committed_any = False

    def push(self, text):
        if not text:
            return
        if self.pending and not self.pending[-1].isspace() and not text[:1].isspace():
            self.pending += " "
        self.pending += text

    def take(self, allowed=True):
        """Return the text to commit and clear the buffer.

        `allowed=False` DISCARDS: better to lose a segment than to commit it
        into a target we are no longer sure of.
        """
        if not self.pending:
            return ""
        text, self.pending = self.pending, ""
        if not allowed:
            return ""
        if self.committed_any and not text[:1].isspace():
            text = " " + text
        return text

    def reset(self):
        self.pending = ""
        self.committed_any = False


# ─────────────────────────────────────────────────────────────────────────
# The engine object the daemon drives
# ─────────────────────────────────────────────────────────────────────────

if HAS_IBUS:

    class SpeaksEngine(IBus.Engine):
        """Receives focus and content-type; sends preedit and commits.

        Deliberately passive about keys: `do_process_key_event` returns False
        so every keystroke passes straight through to the application. That
        single line is why swapping the global engine mid-utterance does not
        take the user's keyboard away.
        """

        __gtype_name__ = "GnomeSpeaksEngine"

        def __init__(self, bus, object_path):
            # has_focus_id decides whether the daemon delivers focus as
            # FocusInId/FocusOutId. Recent ibus uses the _id form; without
            # this property set, focus never arrives at all.
            if hasattr(IBus.Engine.props, "has_focus_id"):
                super().__init__(connection=bus.get_connection(),
                                 object_path=object_path, has_focus_id=True)
            else:
                super().__init__(connection=bus.get_connection(),
                                 object_path=object_path)
            self.purpose = 0
            self.hints = 0
            self.focused = False
            self.saw_content_type = False
            self.client = ""
            self._preediting = False

        # -- key events ---------------------------------------------------

        def do_process_key_event(self, keyval, keycode, state):
            return False  # we synthesize no input; keys are the user's

        # -- focus (both spellings; newer ibus only sends the _id form) ----

        def do_focus_in(self):
            self.do_focus_in_id("", "")

        def do_focus_in_id(self, object_path, client):
            self.focused = True
            self.client = client or ""
            log.debug("IBus focus in (client=%s)", client)

        def do_focus_out(self):
            self.do_focus_out_id("")

        def do_focus_out_id(self, object_path):
            self.focused = False
            self.client = ""
            # Anything provisional dies with the focus it belonged to -- and
            # so does what we knew about the field: the next FocusIn may come
            # from a client that never sends SetContentType.
            self.saw_content_type = False
            self.clear_preedit()
            log.debug("IBus focus out")

        def has_target(self):
            """Focused on a REAL input context (not ibus-daemon\'s fake one)."""
            return bool(self.focused and self.client != FAKE_CLIENT)

        def do_reset(self):
            self.clear_preedit()

        def do_set_content_type(self, purpose, hints):
            self.purpose = purpose
            self.hints = hints
            self.saw_content_type = True
            log.debug("IBus content type purpose=%s hints=%s", purpose, hints)

        # -- text ---------------------------------------------------------

        def is_secure(self):
            return self.purpose in _SECURE_PURPOSES

        def supports_preedit_region(self):
            return bool(self.client_capabilities & IBus.Capabilite.PREEDIT_TEXT)

        def set_preedit(self, text):
            """Replace the volatile region. Successive calls replace, never add."""
            if not text:
                self.clear_preedit()
                return
            if not self.supports_preedit_region():
                # A client with no preedit region silently drops everything we
                # send it -- saying so once beats typing into a black hole.
                log.debug("IBus client has no preedit capability; skipping partial")
                return
            self.update_preedit_text_with_mode(
                IBus.Text.new_from_string(text),
                len(text),                     # cursor at end; CHARS, not bytes
                True,
                IBus.PreeditFocusMode.CLEAR,   # focus-out DISCARDS, never commits
            )
            self._preediting = True

        def clear_preedit(self):
            if not self._preediting:
                return
            try:
                self.update_preedit_text_with_mode(
                    IBus.Text.new_from_string(""), 0, False,
                    IBus.PreeditFocusMode.CLEAR)
                self.hide_preedit_text()
            except Exception:
                log.debug("IBus preedit clear failed", exc_info=True)
            self._preediting = False

        def commit(self, text):
            if not text:
                return False
            # A commit supersedes the volatile tail: clear first, in this order.
            self.clear_preedit()
            self.commit_text(IBus.Text.new_from_string(text))
            return True

    class SpeaksFactory(IBus.Factory):
        """Hands the daemon an engine, and keeps a handle on the one it made."""

        __gtype_name__ = "GnomeSpeaksFactory"

        def __init__(self, bus, on_engine):
            self._bus = bus
            self._on_engine = on_engine
            super().__init__(object_path=IBus.PATH_FACTORY,
                             connection=bus.get_connection())

        def do_create_engine(self, engine_name):
            if engine_name != ENGINE_NAME:
                return super().do_create_engine(engine_name)
            engine = SpeaksEngine(self._bus, ENGINE_PATH)
            self._on_engine(engine)
            return engine

else:  # pragma: no cover - platform dependent
    SpeaksEngine = None
    SpeaksFactory = None


# ─────────────────────────────────────────────────────────────────────────
# The backend
# ─────────────────────────────────────────────────────────────────────────

class IbusInjector(Injector):
    """Commit text as an IBus engine, swapping the global engine per utterance.

    `fallback` is required, not optional: IBus commits into *text input
    contexts*, and some of what this seam is asked to do is not a text commit
    at all (pressing Return). Those delegate rather than being approximated.
    """

    name = "ibus"

    def __init__(self, fallback=None):
        self._fallback = fallback
        self._bus = None
        self._factory = None
        self._engine = None
        self._registered = False
        self._connect_failed = False
        self._lock = threading.RLock()
        self._prior = None
        self._active = False
        # GetGlobalEngine answered "no global engine" on this bus. On GNOME
        # that is the steady state, not a transient, and every further ask is
        # a D-Bus round trip plus one IBUS-WARNING in the journal (#136).
        self._global_engine_unset = False
        self._coalescer = _Coalescer()
        # Why the last acquire() said no: None | "secure" | "no_target" |
        # "unavailable". commit() falls back to ydotool for every reason but
        # "secure" (a password field must get NOTHING, from any backend).
        self._refusal = None
        self._flush_timer = None
        self._watchdog = None

    # ── setup ────────────────────────────────────────────────────────────

    def prepare(self):
        self._ensure_registered()
        if self._fallback is not None:
            # The fallback still owns keystrokes; let it detect its tooling.
            self._fallback.prepare()

    def _ensure_registered(self):
        """Connect and register the component. Idempotent; safe to call often."""
        if self._registered or self._connect_failed or not HAS_IBUS:
            return self._registered
        with self._lock:
            if self._registered or self._connect_failed:
                return self._registered
            try:
                IBus.init()
                bus = IBus.Bus()
                if not bus.is_connected():
                    log.warning("IBus daemon not reachable; injection stays on "
                                "the fallback backend")
                    self._connect_failed = True
                    return False
                component = IBus.Component.new(
                    COMPONENT_NAME, "GNOME Speaks dictation", "1.0",
                    "GPL-3.0-or-later", "JP Hein", "", "", "gnome-speaks")
                component.add_engine(IBus.EngineDesc.new(
                    ENGINE_NAME, "GNOME Speaks", "Voice dictation", "en",
                    "GPL-3.0-or-later", "JP Hein", "", ENGINE_LAYOUT))
                if not bus.register_component(component):
                    log.warning("IBus refused our component registration")
                    self._connect_failed = True
                    return False
                self._factory = SpeaksFactory(bus, self._on_engine_created)
                self._bus = bus
                self._global_engine_unset = False   # a new bus gets asked afresh
                self._registered = True
                log.info("IBus component registered (engine %s, layout %s)",
                         ENGINE_NAME, ENGINE_LAYOUT)
                return True
            except Exception:
                log.warning("IBus registration failed", exc_info=True)
                self._connect_failed = True
                return False

    def _on_engine_created(self, engine):
        self._engine = engine
        log.debug("IBus engine object created")

    # ── capability ───────────────────────────────────────────────────────

    def available(self):
        if not HAS_IBUS:
            return False
        if not self._ensure_registered():
            return False
        try:
            return bool(self._bus and self._bus.is_connected())
        except Exception:
            return False

    def supports_preedit(self):
        return True

    # ── session lifecycle ────────────────────────────────────────────────

    def purpose_known(self):
        """True when a field is focused and its purpose is not PASSWORD/PIN.

        Read the name as "not known secure", not "known safe".  FREE_FORM is 0
        -- every ordinary editor, textarea and terminal -- so the test can
        never be "is purpose truthy"; that refused every plain text field
        (#55).  saw_content_type is kept as the honest form of the question,
        but ibus-daemon 1.5.34 forwards SetContentType to a fresh engine
        unconditionally, so in practice it is True whenever we are focused, and
        a client that declared nothing is indistinguishable from one that
        declared FREE_FORM: both arrive as (0, 0).  Undeclared fields pass.
        """
        eng = self._engine
        return bool(eng is not None and eng.focused and eng.saw_content_type
                    and not eng.is_secure())

    def acquire(self):
        """Become the global engine for one utterance.

        Returns False only when we must NOT type: the swap was refused, or the
        target is a password field. A slow or absent FocusIn is the ordinary
        field case, not a refusal -- treating it as one would break dictation
        into perfectly normal windows.
        """
        if not self.available():
            self._refusal = "unavailable"
            return False
        with self._lock:
            if self._active:
                return not self._is_secure()
            self._refusal = None
            prior_name = None
            if not self._global_engine_unset:
                try:
                    prior = self._bus.get_global_engine()
                    prior_name = prior.get_name() if prior else None
                except Exception:
                    # "No global engine" is the COMMON case on GNOME, not an
                    # error: the shell owns input sources and often leaves
                    # the daemon's global engine unset. Not a reason to give
                    # up on having somewhere to go back to.
                    log.debug("GetGlobalEngine unavailable", exc_info=True)
                    prior_name = None
                if not prior_name:
                    # Asked, and the daemon has none. Do not ask again on this
                    # bus: the answer comes from input-sources below either
                    # way, and libibus g_warning()s on every empty reply --
                    # one journal line per utterance before #136. A layout
                    # switch still lands, because the input-sources read in
                    # derive_restore_target() is not cached.
                    self._global_engine_unset = True
            if prior_name == ENGINE_NAME:
                prior_name = None  # never record ourselves as the way back
            if not prior_name:
                prior_name = derive_restore_target(self._bus)
                if prior_name:
                    log.debug("No global engine set; restore target derived "
                              "from input-sources: %r", prior_name)
                else:
                    log.warning("No global engine and no derivable input "
                                "source: a dictation session may not be able "
                                "to hand the input method back")
            # Persist BEFORE the swap: a crash in between is the whole reason
            # this file exists, and a breadcrumb written afterwards is a
            # breadcrumb that is missing exactly when it is needed.
            if prior_name:
                write_prior_engine(prior_name)
            self._prior = prior_name
            try:
                if not self._bus.set_global_engine(ENGINE_NAME):
                    log.warning("IBus refused SetGlobalEngine; not typing")
                    self._refusal = "unavailable"
                    clear_prior_engine()
                    self._prior = None
                    return False
            except Exception:
                log.warning("IBus SetGlobalEngine failed", exc_info=True)
                self._refusal = "unavailable"
                clear_prior_engine()
                self._prior = None
                return False
            self._active = True
            self._start_watchdog()

        # Wait for the daemon to push focus at us, then let SetContentType
        # settle behind it -- a bare read loses that race.
        deadline = time.monotonic() + FOCUS_WAIT
        while time.monotonic() < deadline:
            eng = self._engine
            if eng is not None and eng.focused:
                break
            time.sleep(0.01)
        time.sleep(CONTENT_TYPE_GRACE)

        if self._is_secure():
            log.info("IBus target is a password field; refusing to type")
            self._refusal = "secure"
            self.cancel()
            return False
        eng = self._engine
        if eng is not None and eng.focused and getattr(eng, "client", "") == FAKE_CLIENT:
            # No input context has focus (a pointer tap on shell chrome does
            # this). A commit here lands nowhere and is lost silently -- so
            # hand the whole utterance to the keystroke backend instead (#109).
            log.info("IBus focus is the daemon's fake context (no text field "
                     "focused); typing this utterance via the fallback backend")
            self._refusal = "no_target"
            self.cancel()
            return False
        self._refusal = None
        return True

    def _is_secure(self):
        eng = self._engine
        return bool(eng is not None and eng.is_secure())

    def _ensure_session(self):
        if self._refusal == "no_target":
            return False  # decided for this utterance; end() clears it
        if self._active:
            # Re-read the purpose every time: SetContentType can arrive late,
            # and focus can move into a password field mid-session.
            if self._is_secure():
                log.info("IBus focus moved into a password field; discarding")
                # "secure" outranks every fallback: the keystroke backend must
                # not type what IBus just refused (the verifier's mid-session
                # password case caught exactly that).
                self._refusal = "secure"
                self.cancel()
                return False
            return True
        return self.acquire()

    def end(self):
        """Finish cleanly: flush what is buffered, then hand the IME back."""
        with self._lock:
            self._refusal = None
            self._cancel_timers()
            if self._active:
                self._flush(allowed=True)
                eng = self._engine
                if eng is not None:
                    eng.clear_preedit()
            self._coalescer.reset()
            self._restore()

    def cancel(self):
        """Abandon: discard anything provisional and hand the IME back."""
        with self._lock:
            self._cancel_timers()
            self._coalescer.reset()
            eng = self._engine
            if eng is not None:
                eng.clear_preedit()
            self._restore()

    def _restore(self):
        """Put the user's engine back. Restore-once; safe to call repeatedly."""
        if not self._active:
            return
        self._active = False
        prior, self._prior = self._prior, None
        if not prior:
            # Late derivation: acquire may have found nothing, but leaving the
            # session on OUR engine is not an option -- that is a stranded
            # input method, which is the failure this whole backend exists to
            # avoid. Try again now rather than "standing down".
            prior = derive_restore_target(self._bus)
        try:
            if prior:
                self._bus.set_global_engine(prior)
                log.debug("IBus global engine restored to %r", prior)
            else:
                # No target at all. Say so loudly: the user is sitting on our
                # engine and nothing here can move them off it.
                log.warning("IBus session ended with no restore target; the "
                            "input method may be left on %s. Recover with: "
                            "ibus engine <your-input-source>", ENGINE_NAME)
                return
        except Exception:
            log.warning("IBus restore failed; the breadcrumb file will be "
                        "used at next service start", exc_info=True)
            return
        clear_prior_engine()

    def recover(self):
        self.cancel()
        restore_prior_engine("recover")
        if self._fallback is not None:
            self._fallback.recover()

    # ── watchdog ─────────────────────────────────────────────────────────

    def _start_watchdog(self):
        self._stop_watchdog()
        self._watchdog = threading.Timer(SESSION_MAX_SECONDS, self._on_watchdog)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _stop_watchdog(self):
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    def _on_watchdog(self):
        log.warning("IBus session exceeded %ss; force-restoring the input "
                    "method", SESSION_MAX_SECONDS)
        self.cancel()

    # ── commit path ──────────────────────────────────────────────────────

    def _cancel_timers(self):
        self._stop_watchdog()
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None

    def _arm_flush(self):
        if self._flush_timer is not None:
            self._flush_timer.cancel()
        self._flush_timer = threading.Timer(COALESCE_DELAY, self._timed_flush)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _timed_flush(self):
        with self._lock:
            self._flush_timer = None
            self._flush(allowed=True)

    def _flush(self, allowed=True):
        text = self._coalescer.take(allowed=allowed and not self._is_secure())
        if not text:
            return False
        eng = self._engine
        if eng is None:
            log.warning("IBus flush with no engine object; text dropped")
            return False
        try:
            eng.commit(text)
            self._coalescer.committed_any = True
            return True
        except Exception:
            log.warning("IBus commit failed", exc_info=True)
            return False

    # ── Injector interface ───────────────────────────────────────────────

    def commit(self, text):
        if not text:
            return False
        if not self._ensure_session():
            return self._fallback_text(text)
        with self._lock:
            self._coalescer.push(text)
            self._arm_flush()
        return True

    def _fallback_text(self, text):
        """Type through the keystroke backend when IBus has no usable target.

        Never for a password field: "secure" means nothing may be typed by
        any backend. Everything else -- fake context, daemon unavailable,
        swap refused -- keeps the seam's promise of falling back to ydotool,
        never to nothing.
        """
        if self._refusal == "secure" or self._fallback is None:
            return False
        return bool(self._fallback.commit(text))

    def type_text(self, text):
        # TEXT path: goes through the same coalesced commit so the
        # whitespace-join rule can stop it doubling a separator.
        return self.commit(text)

    def finalize(self, text):
        # The partials only ever lived in the pre-edit, and end() clears that
        # region rather than committing it -- so a keep-live-text utterance
        # must be committed here or it never reaches the field (#45). The
        # engine's commit clears the pre-edit first, so nothing doubles.
        return self.commit(text)

    def press_enter(self):
        # A KEY EVENT. commit_text("\n") puts a newline character in the field
        # and no shell ever runs the command, so this must not be a commit.
        if self._fallback is None:
            log.warning("No fallback backend for press_enter; Enter not sent")
            return None
        return self._fallback.press_enter()

    def set_preedit(self, text):
        if text and not self._ensure_session():
            return
        eng = self._engine
        if eng is None:
            return
        try:
            eng.set_preedit(text)
        except Exception:
            log.debug("IBus preedit update failed", exc_info=True)

    def replace_text(self, old_text, new_text):
        # Replacement is what a preedit region is for: no diffing, no prefix
        # arithmetic, no backspaces.
        self.set_preedit(new_text)

    def send_backspaces(self, count):
        # Retracting provisional text is one idempotent call here; there is
        # nothing committed to take back.
        self.set_preedit("")

    def paste(self, text):
        # The clipboard round-trip existed to work around ydotool dropping
        # characters in long bursts. A D-Bus commit has no such failure mode.
        return self.commit(text)
