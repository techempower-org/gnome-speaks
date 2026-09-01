# SPDX-License-Identifier: GPL-3.0-or-later
# GNOME Speaks — text injection backend contract
# Copyright (C) 2025 JP Hein
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""The `Injector` seam: every path that puts text in front of the cursor.

Two backends implement this:

  * `YdotoolInjector` (in gnome-speaks-service.py) synthesizes key events on a
    uinput device. Always reachable — see below.
  * `IbusInjector` (in ibus_injector.py) commits text over D-Bus as an IBus
    engine. No key events, so nothing can be left stuck.

The contract is deliberately shaped so the STT cycle never asks which backend
it has. `replace_text`, `send_backspaces` and `paste` mean *intent* ("revise
the live text", "retract it", "deliver this block"), not mechanism, and each
backend spends that intent its own way: ydotool backspaces and retypes, IBus
replaces a pre-edit region. The one thing callers must branch on is
`supports_preedit()`, and only if they care how the text looks while it is
still provisional.

**ydotool never goes away** (spec 5.4). IBus commits into *text input
contexts*; a keystroke is not a text commit, a clipboard paste is not a text
commit, and a non-GNOME session has no IBus at all. `press_enter` is the
first-class example: `commit_text("\\n")` inserts a newline character into the
field, it does not press Return, so a shell never runs the command.
"""


class Injector:
    """Backend-agnostic text injection.

    Every method has a safe default, so a partial backend degrades rather than
    crashing, and adding a method here does not break existing backends.
    """

    name = "base"

    # ── lifecycle ────────────────────────────────────────────────────────

    def prepare(self):
        """One-time backend startup. Called off the main thread at boot."""

    def acquire(self):
        """Bind an injection target for one utterance.

        False means refuse — no target, or a target we must not type into
        (a password field). Backends with no session concept return True.
        """
        return True

    def end(self):
        """Finish the current utterance cleanly. MUST be idempotent."""

    def cancel(self):
        """Abandon the current utterance, discarding anything provisional.

        MUST be idempotent, and MUST leave the desktop no worse than it found
        it even when called from an error path.
        """

    def recover(self):
        """Clear wedged backend state. Called on shutdown and after faults."""

    # ── capability ───────────────────────────────────────────────────────

    def available(self):
        """True if this backend can put text at the cursor right now."""
        return False

    def supports_preedit(self):
        """True if provisional text goes to a volatile pre-edit region.

        False means provisional text is really typed and must be really
        retracted, which is what makes `send_backspaces` meaningful.
        """
        return False

    # ── injection ────────────────────────────────────────────────────────

    def commit(self, text):
        """Put final `text` at the cursor. Returns True on success."""
        raise NotImplementedError

    def type_raw(self, text):
        """Put `text` at the cursor with no focus-settle delay or fallback."""
        raise NotImplementedError

    def press_enter(self):
        """Press Return — a KEY EVENT, never a text commit.

        Backends that cannot generate key events must delegate this, not
        approximate it with a newline character.
        """
        raise NotImplementedError

    def send_backspaces(self, count):
        """Retract `count` characters of provisional text already shown."""
        raise NotImplementedError

    def replace_text(self, old_text, new_text):
        """Revise the provisional text from `old_text` to `new_text`."""
        raise NotImplementedError

    def set_preedit(self, text):
        """Show `text` as provisional; "" clears it.

        Only meaningful when `supports_preedit()` is True.
        """
        raise NotImplementedError

    def paste(self, text):
        """Deliver `text` as one block. Returns True on success."""
        raise NotImplementedError
