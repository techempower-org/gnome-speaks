// SPDX-License-Identifier: GPL-3.0-or-later
// GNOME Speaks — TTS/STT floating badge for GNOME Shell
// Copyright (C) 2025 JP Hein
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.

import GLib from 'gi://GLib';
import St from 'gi://St';
import Atk from 'gi://Atk';
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

// St.BoxLayout:vertical was deprecated in GNOME 48 in favour of :orientation,
// and upstream has it slated for removal. :orientation does not exist before
// 48, and metadata.json still claims 46/47 — so detect once at load and let
// every box construction speak whichever dialect this shell understands.
// `pack-start` is already gone in GNOME 50; `vertical` is next.
const HAS_ORIENTATION = 'orientation' in St.BoxLayout.prototype;

/**
 * Construct-time orientation props for an St.BoxLayout, portable across 46-50.
 *
 * @param {boolean} vertical - true for a column, false for a row
 * @returns {object} props to spread into the St.BoxLayout constructor
 */
function boxOrientation(vertical) {
    if (HAS_ORIENTATION) {
        return {
            orientation: vertical
                ? Clutter.Orientation.VERTICAL
                : Clutter.Orientation.HORIZONTAL,
        };
    }
    return {vertical};
}

const DBUS_NAME = 'org.gnome.Speaks';
const DBUS_PATH = '/org/gnome/Speaks';
const DBUS_INTERFACE = 'org.gnome.Speaks';

const DBUS_XML = `
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
    <method name="Talk">
      <arg direction="in" type="s" name="text"/>
      <arg direction="out" type="s" name="reply"/>
    </method>
    <method name="GetContinuousDictation">
      <arg direction="out" type="b" name="enabled"/>
    </method>
    <method name="GetConversationMode">
      <arg direction="out" type="b" name="enabled"/>
    </method>
    <method name="ToggleTerminalMode">
      <arg direction="out" type="b" name="enabled"/>
    </method>
    <method name="GetTerminalMode">
      <arg direction="out" type="b" name="enabled"/>
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
</node>`;

const GnomeSpeaksProxy = Gio.DBusProxy.makeProxyWrapper(DBUS_XML);

// Exported BY the extension (the service owns org.gnome.Speaks; this name is
// ours). GNOME locked down Shell.Eval/Introspect, so the Python service can
// no longer ask the compositor anything — this interface runs inside the
// Shell and answers on its behalf (gnome-speaks#7).
const DESKTOP_DBUS_XML = `<node>
  <interface name="org.gnome.Speaks.Desktop">
    <method name="GetFocusedApp">
      <arg direction="out" type="s" name="wm_class"/>
      <arg direction="out" type="s" name="title"/>
    </method>
  </interface>
</node>`;

const States = {
    IDLE: 'idle',
    LISTENING: 'listening',
    PROCESSING: 'processing',
    SPEAKING: 'speaking',
};

const STATE_CONFIG = {
    [States.IDLE]: {
        iconName: 'audio-input-microphone-symbolic',
        label: '',
        styleClass: 'gnome-speaks-idle',
        showLabel: false,
        // What a screen reader says instead of the icon. The badge's own
        // label is empty while idle, so without this it announces nothing.
        accessibleName: 'GNOME Speaks — idle, activate to start listening',
    },
    [States.LISTENING]: {
        iconName: 'audio-input-microphone-symbolic',
        label: 'Listening...',
        styleClass: 'gnome-speaks-listening',
        showLabel: true,
        accessibleName: 'GNOME Speaks — listening, activate to stop',
    },
    [States.PROCESSING]: {
        iconName: 'process-working-symbolic',
        label: 'Processing...',
        styleClass: 'gnome-speaks-processing',
        showLabel: true,
        accessibleName: 'GNOME Speaks — processing, activate to cancel',
    },
    [States.SPEAKING]: {
        iconName: 'audio-speakers-symbolic',
        label: 'Speaking...',
        styleClass: 'gnome-speaks-speaking',
        showLabel: true,
        accessibleName: 'GNOME Speaks — speaking, activate to stop',
    },
};

// Word-reveal markup colors (Pango span attributes)
const WORD_COLOR_SETTLED = '#e8e0f0';      // soft cream — matches subtitle text
const WORD_COLOR_NEW = '#88ccff';          // bright blue — new words
const WORD_COLOR_CORRECTED = '#ffcc66';    // warm amber — corrected words

export default class GnomeSpeaksExtension extends Extension {
    enable() {
        try {
            this._enable();
        } catch (e) {
            console.error(`[GNOME Speaks] enable() failed: ${e.message}\n${e.stack}`);
        }
    }

    _enable() {
        this._destroyed = false;
        this._state = States.IDLE;
        this._proxy = null;
        this._proxyReady = false;
        this._proxyPending = false;
        this._proxySignals = [];
        this._signals = [];
        this._timeouts = [];
        this._pulseTransition = null;
        this._isDragging = false;
        this._dragStartX = 0;
        this._dragStartY = 0;
        this._dragBadgeStartX = 0;
        this._dragBadgeStartY = 0;
        this._badgeVisible = true;
        this._audioLevel = 0;
        this._lastAudioLevelTime = 0;
        this._lastPartialTime = 0;
        this._lastPartialText = null;
        this._lastRenderedPartial = null;
        this._pendingPartialText = null;
        this._partialDebounceId = null;
        this._lastSubtitleUpdateTime = 0;
        this._pendingSubtitleReveal = null;
        this._subtitleDebounceId = null;
        this._subtitleText = '';
        this._subtitleVisible = false;
        this._previousWords = [];
        this._wordHighlights = [];
        this._pendingPartialMarkup = null;
        this._lastVadTime = 0;
        // Focus bookkeeping for the popups: the rows we can walk with the
        // arrows, and the actor that gets focus back when they close.
        this._chronicleLines = [];
        this._chronicleFocusReturn = null;
        this._contextMenuItems = [];
        this._contextFocusReturn = null;
        this._ctrlAltTabRegistered = false;
        this._settings = this.getSettings();

        // Restore persisted badge position
        let savedX = this._settings.get_int('badge-position-x');
        let savedY = this._settings.get_int('badge-position-y');
        this._customPosition = (savedX >= 0 && savedY >= 0)
            ? {x: savedX, y: savedY} : null;

        this._createBadge();
        this._createSubtitleOverlay();
        this._createPanelIndicator();
        this._positionBadge();
        this._connectLayoutSignals();
        this._registerKeybindings();
        this._initProxy();
        this._connectNotificationReader();
        this._exportDesktopInterface();

        // Watch for service restarts — re-sync state when the bus name reappears
        this._busWatchId = Gio.bus_watch_name(
            Gio.BusType.SESSION,
            DBUS_NAME,
            Gio.BusNameWatcherFlags.NONE,
            () => {
                if (this._destroyed) return;
                // Name appeared (service started or restarted)
                if (this._proxyReady)
                    this._syncState();
                else if (!this._proxyPending)
                    this._initProxy();
            },
            () => {
                if (this._destroyed) return;
                // Name vanished (service stopped) — reset badge to idle
                this._setState(States.IDLE);
            },
        );
    }

    disable() {
        try {
            this._disable();
        } catch (e) {
            console.error(`[GNOME Speaks] disable() failed: ${e.message}\n${e.stack}`);
        }
    }

    _disable() {
        this._destroyed = true;

        this._unexportDesktopInterface();

        if (this._busWatchId) {
            Gio.bus_unwatch_name(this._busWatchId);
            this._busWatchId = 0;
        }

        this._endDrag();
        this._partialDebounceId = null;
        this._subtitleDebounceId = null;
        this._cancelAllTimeouts();
        this._stopPulse();
        this._disconnectNotificationReader();
        this._removeKeybindings();
        this._disconnectLayoutSignals();

        // Destroy UI actors BEFORE disconnecting proxy — actor destruction
        // can trigger GC, and we need the proxy reference alive so GC
        // doesn't try to finalize GjsDBusImplementation during the sweep.
        this._destroySubtitleOverlay();
        this._destroyPanelIndicator();
        this._destroyBadge();
        this._disconnectProxy();

        this._state = null;
        this._proxyReady = false;
        this._settings = null;
    }

    // -- Desktop D-Bus export ------------------------------------------------

    _exportDesktopInterface() {
        try {
            this._desktopDbus = Gio.DBusExportedObject.wrapJSObject(
                DESKTOP_DBUS_XML, this);
            this._desktopDbus.export(Gio.DBus.session,
                '/org/gnome/Speaks/Desktop');
            this._desktopNameId = Gio.DBus.session.own_name(
                'org.gnome.Speaks.Desktop',
                Gio.BusNameOwnerFlags.NONE, null, null);
        } catch (e) {
            console.warn(`[GNOME Speaks] Desktop D-Bus export failed: ${e.message}`);
            this._desktopDbus = null;
            this._desktopNameId = 0;
        }
    }

    _unexportDesktopInterface() {
        if (this._desktopNameId) {
            Gio.bus_unown_name(this._desktopNameId);
            this._desktopNameId = 0;
        }
        if (this._desktopDbus) {
            try {
                this._desktopDbus.unexport();
            } catch (e) {
                // already gone — fine
            }
            this._desktopDbus = null;
        }
    }

    GetFocusedApp() {
        const win = global.display.get_focus_window();
        if (!win)
            return ['', ''];
        return [win.get_wm_class() || '', win.get_title() || ''];
    }

    // -- Keyboard shortcuts ------------------------------------------------

    _registerKeybindings() {
        Main.wm.addKeybinding(
            'toggle-listening-shortcut',
            this._settings,
            Meta.KeyBindingFlags.NONE,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW,
            () => {
                if (this._state === States.LISTENING)
                    this._callMethod('StopListening');
                else if (this._state === States.IDLE)
                    this._callMethod('StartListening');
            }
        );

        Main.wm.addKeybinding(
            'speak-clipboard-shortcut',
            this._settings,
            Meta.KeyBindingFlags.NONE,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW,
            () => this._callMethod('SpeakClipboard')
        );

        Main.wm.addKeybinding(
            'read-selection-shortcut',
            this._settings,
            Meta.KeyBindingFlags.NONE,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW,
            () => this._callMethod('SpeakSelection')
        );

        Main.wm.addKeybinding(
            'toggle-voice-quality-shortcut',
            this._settings,
            Meta.KeyBindingFlags.NONE,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW,
            () => this._toggleVoiceQuality()
        );
    }

    _removeKeybindings() {
        Main.wm.removeKeybinding('toggle-listening-shortcut');
        Main.wm.removeKeybinding('speak-clipboard-shortcut');
        Main.wm.removeKeybinding('read-selection-shortcut');
        Main.wm.removeKeybinding('toggle-voice-quality-shortcut');
    }

    // -- Notification reader -----------------------------------------------

    _connectNotificationReader() {
        this._notifSignals = [];

        this._notifSourceAddedId = Main.messageTray.connect('source-added', (tray, source) => {
            if (this._destroyed) return;
            try {
                let notifAddedId = source.connect('notification-added', (src, notification) => {
                    if (this._destroyed) return;
                    this._onNotification(notification);
                });
                this._notifSignals.push({obj: source, id: notifAddedId});
            } catch (e) {
                // Source may already be disposed during shell init/restart
            }
        });

        // Proactively drop references when sources are removed, before GC disposes them
        this._notifSourceRemovedId = Main.messageTray.connect('source-removed', (tray, source) => {
            if (!this._notifSignals) return;
            this._notifSignals = this._notifSignals.filter(sig => {
                if (sig.obj === source) {
                    try { source.disconnect(sig.id); } catch (e) { /* ok */ }
                    return false;
                }
                return true;
            });
        });
    }

    _disconnectNotificationReader() {
        if (this._notifSourceAddedId) {
            Main.messageTray.disconnect(this._notifSourceAddedId);
            this._notifSourceAddedId = null;
        }
        if (this._notifSourceRemovedId) {
            Main.messageTray.disconnect(this._notifSourceRemovedId);
            this._notifSourceRemovedId = null;
        }
        if (this._notifSignals) {
            for (let sig of this._notifSignals) {
                try { sig.obj.disconnect(sig.id); } catch (e) { /* disposed, ok */ }
            }
            this._notifSignals = null;
        }
    }

    _getConfigFlag(key, defaultVal = true) {
        // Cached config read — refreshes at most once per 10 seconds
        let now = GLib.get_monotonic_time();
        if (!this._notifConfigCache || (now - this._notifConfigCacheTime) > 10000000) {
            let configPath = GLib.build_filenamev([
                GLib.get_home_dir(), '.config', 'speech-to-cli', 'config.json',
            ]);
            try {
                let [ok, contents] = GLib.file_get_contents(configPath);
                if (ok) {
                    let decoder = new TextDecoder('utf-8');
                    this._notifConfigCache = JSON.parse(decoder.decode(contents));
                } else {
                    this._notifConfigCache = {};
                }
            } catch (e) {
                this._notifConfigCache = {};
            }
            this._notifConfigCacheTime = now;
        }
        let val = this._notifConfigCache[key];
        return val !== undefined ? val : defaultVal;
    }

    _onNotification(notification) {
        if (!this._getConfigFlag('read_notifications', false))
            return;

        let title = notification.title || '';
        let body = notification.body || '';
        let text = title;
        if (body)
            text += `. ${body}`;
        if (text && this._state === States.IDLE)
            this._callMethod('Speak', text);
    }

    _createBadge() {
        this._icon = new St.Icon({
            icon_name: STATE_CONFIG[States.IDLE].iconName,
            icon_size: 22,
            x_expand: true,
            x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.CENTER,
        });

        this._label = new St.Label({
            text: '',
            // Named, not inferred: the stylesheet used to reach this label
            // through `.gnome-speaks-badge StLabel`, which also swept up
            // every other label the badge will ever contain.
            style_class: 'gnome-speaks-badge-label',
            y_align: Clutter.ActorAlign.CENTER,
            visible: false,
        });

        this._badge = new St.BoxLayout({
            style_class: 'gnome-speaks-badge gnome-speaks-idle',
            reactive: true,
            can_focus: true,
            track_hover: true,
            x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.CENTER,
            ...boxOrientation(false),
            accessible_role: Atk.Role.PUSH_BUTTON,
            accessible_name: STATE_CONFIG[States.IDLE].accessibleName,
        });

        this._badge.add_child(this._icon);
        this._badge.add_child(this._label);

        // Pills — hidden in idle, shown when active
        this._conversationMode = false;
        this._continuousMode = false;
        this._terminalMode = false;

        this._qualityPill = this._createPill('✦', 'gnome-speaks-quality-hd',
            'Voice quality', () => this._toggleVoiceQuality());
        this._modePill = this._createPill('✏️', 'gnome-speaks-mode-dict',
            'Speech mode', () => this._toggleMode());
        this._continuousPill = this._createPill('🔄', 'gnome-speaks-pill-off',
            'Continuous dictation', () => this._toggleContinuous());
        this._terminalPill = this._createPill('>', 'gnome-speaks-pill-off',
            'Terminal mode', () => this._toggleTerminal());

        this._badge.add_child(this._qualityPill);
        this._badge.add_child(this._modePill);
        this._badge.add_child(this._continuousPill);
        this._badge.add_child(this._terminalPill);

        // The Chronicle rune — deliberately NOT in this._pills: the mode
        // pills come and go with activity, but the ledger of what was said
        // is exactly what you reach for while idle, so it never hides.
        this._chroniclePill = this._createPill('📜', 'gnome-speaks-pill-chronicle',
            'The Chronicle — recent speech',
            fromKeyboard => this._toggleChronicleScroll(fromKeyboard));
        this._badge.add_child(this._chroniclePill);

        this._pills = [this._qualityPill, this._modePill, this._continuousPill, this._terminalPill];
        for (let pill of this._pills)
            pill.hide();

        this._badge.set_pivot_point(0.5, 0.5);

        let pressId = this._badge.connect('button-press-event', (actor, event) => {
            let button = event.get_button();
            if (button === 3) {
                this._showContextMenu();
                return Clutter.EVENT_STOP;
            }
            if (button === 1) {
                let [stageX, stageY] = event.get_coords();
                this._dragStartX = stageX;
                this._dragStartY = stageY;
                this._dragBadgeStartX = actor.x;
                this._dragBadgeStartY = actor.y;
                this._isDragging = false;
                this._dragButton = button;
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });
        this._signals.push({obj: this._badge, id: pressId});

        let motionId = this._badge.connect('motion-event', (actor, event) => {
            if (this._dragButton !== 1)
                return Clutter.EVENT_PROPAGATE;

            let [stageX, stageY] = event.get_coords();
            let dx = stageX - this._dragStartX;
            let dy = stageY - this._dragStartY;

            if (!this._isDragging && (Math.abs(dx) > 5 || Math.abs(dy) > 5)) {
                this._isDragging = true;
                // Grab all input so drag continues even when cursor
                // leaves the badge bounds
                this._dragGrab = global.stage.grab(this._badge);
            }

            if (this._isDragging) {
                actor.set_position(
                    this._dragBadgeStartX + dx,
                    this._dragBadgeStartY + dy
                );
                this._customPosition = {x: actor.x, y: actor.y};
                this._positionWaveform();
                this._positionSubtitleOverlay();
                if (this._settings) {
                    this._settings.set_int('badge-position-x', Math.round(actor.x));
                    this._settings.set_int('badge-position-y', Math.round(actor.y));
                }
            }
            return Clutter.EVENT_STOP;
        });
        this._signals.push({obj: this._badge, id: motionId});

        let releaseId = this._badge.connect('button-release-event', () => {
            // Always end drag state on any release — the grab routes
            // all events here, so we must unconditionally clean up
            let wasDragging = this._isDragging;
            this._endDrag();

            if (!wasDragging) {
                this._onBadgeClicked();
            }
            return Clutter.EVENT_STOP;
        });
        this._signals.push({obj: this._badge, id: releaseId});

        let keyId = this._badge.connect('key-press-event', (actor, event) => {
            let symbol = event.get_key_symbol();

            // Escape furls whatever is open, from anywhere in the badge —
            // including while a pill holds focus, which is exactly where a
            // keyboard user is standing when they open the Chronicle.
            if (symbol === Clutter.KEY_Escape) {
                if (this._chronicleScroll || this._contextMenu) {
                    this._destroyChronicleScroll();
                    this._destroyContextMenu();
                    return Clutter.EVENT_STOP;
                }
                return Clutter.EVENT_PROPAGATE;
            }

            // Everything else belongs to the focused actor. A focused pill is
            // an St.Button and answers Enter/Space itself; the badge must not
            // reach past it and start listening on the same keystroke.
            if (!actor.has_key_focus())
                return Clutter.EVENT_PROPAGATE;

            if (symbol === Clutter.KEY_Return || symbol === Clutter.KEY_KP_Enter ||
                symbol === Clutter.KEY_space) {
                this._onBadgeClicked();
                return Clutter.EVENT_STOP;
            }
            if (symbol === Clutter.KEY_Menu) {
                this._showContextMenu(true);
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });
        this._signals.push({obj: this._badge, id: keyId});

        // Waveform + timeout — vertical stack below badge
        this._waveformLevels = new Array(32).fill(0);
        this._waveformContainer = new St.BoxLayout({
            style_class: 'gnome-speaks-waveform-wrapper',
            reactive: false,
            can_focus: false,
            ...boxOrientation(true),
            opacity: 0,
        });

        // Mirrored bar rows — top grows down, bottom grows up
        this._waveformBars = [];
        this._waveformBarsTop = [];
        this._waveformRowTop = new St.BoxLayout({
            style_class: 'gnome-speaks-waveform',
            ...boxOrientation(false),
            y_align: Clutter.ActorAlign.END, // bars grow downward from bottom of top row
        });
        this._waveformRowTop.set_height(24);
        this._waveformRow = new St.BoxLayout({
            style_class: 'gnome-speaks-waveform',
            ...boxOrientation(false),
            y_align: Clutter.ActorAlign.START, // bars grow upward from top of bottom row
        });
        this._waveformRow.set_height(24);
        for (let i = 0; i < 32; i++) {
            let barTop = new St.Bin({
                style_class: 'gnome-speaks-waveform-bar',
                reactive: false,
                y_align: Clutter.ActorAlign.END,
                x_expand: true,
            });
            let barBot = new St.Bin({
                style_class: 'gnome-speaks-waveform-bar',
                reactive: false,
                y_align: Clutter.ActorAlign.START,
                x_expand: true,
            });
            this._waveformBarsTop.push(barTop);
            this._waveformBars.push(barBot);
            this._waveformRowTop.add_child(barTop);
            this._waveformRow.add_child(barBot);
        }
        this._waveformContainer.add_child(this._waveformRowTop);
        this._waveformContainer.add_child(this._waveformRow);

        // Silence fade: waveform dims over time when no speech
        this._lastSpeechTime = 0;
        this._silenceFadeActive = false;

        Main.uiGroup.add_child(this._waveformContainer);

        // VAD indicator — glowing green dot to the right of the badge
        this._vadDot = new St.Bin({
            style_class: 'gnome-speaks-vad-dot',
            reactive: false,
            width: 12,
            height: 12,
            opacity: 0,
        });
        Main.uiGroup.add_child(this._vadDot);

        // No affectsInputRegion: GNOME 49+ tracks input regions from
        // reactive actors automatically and 50 REJECTS the param; on 46-48
        // it defaulted to true anyway, so omitting it changes nothing.
        Main.layoutManager.addTopChrome(this._badge, {
            trackFullscreen: false,
        });

        // Chrome is invisible to the keyboard unless it joins the
        // Ctrl+Alt+Tab ring — focusable pills are worth nothing if there is
        // no door into the badge. Guarded: a shell that ever renames this
        // manager should cost us a keyboard shortcut, not the extension.
        try {
            Main.ctrlAltTabManager.addGroup(this._badge, 'GNOME Speaks',
                'audio-input-microphone-symbolic');
            this._ctrlAltTabRegistered = true;
        } catch (e) {
            this._ctrlAltTabRegistered = false;
            console.debug(`[GNOME Speaks] Ctrl+Alt+Tab registration skipped: ${e.message}`);
        }
    }

    _endDrag() {
        this._dragButton = 0;
        this._isDragging = false;
        if (this._dragGrab) {
            this._dragGrab.dismiss();
            this._dragGrab = null;
        }
    }

    _toggleVoiceQuality() {
        if (!this._proxy) {
            // Local toggle for testing without service
            this._voiceQuality = this._voiceQuality === 'hd' ? 'fast' : 'hd';
            this._updateQualityPill();
            return;
        }
        this._proxy.ToggleVoiceQualityRemote((result, error) => {
            if (this._destroyed || error) return;
            let quality = result[0];
            this._voiceQuality = quality;
            this._updateQualityPill();
            if (this._menuVoiceQualityItem)
                this._menuVoiceQualityItem.label.text = quality === 'hd'
                    ? 'Voice: HD' : 'Voice: Fast';
        });
    }

    _updateQualityPill() {
        if (!this._qualityPill) return;
        let isHD = this._voiceQuality === 'hd';
        this._setPillState(this._qualityPill, isHD ? '✦ HD' : '⚡ Fast',
            isHD ? 'Voice quality: HD — activate for Fast'
                : 'Voice quality: Fast — activate for HD');
        this._qualityPill.remove_style_class_name(
            isHD ? 'gnome-speaks-quality-fast' : 'gnome-speaks-quality-hd');
        this._qualityPill.add_style_class_name(
            isHD ? 'gnome-speaks-quality-hd' : 'gnome-speaks-quality-fast');
    }

    _toggleMode() {
        if (!this._proxy) {
            // Local toggle for testing without service
            this._conversationMode = !this._conversationMode;
            this._updateModePill();
            return;
        }
        this._proxy.ToggleConversationModeRemote((result, error) => {
            if (this._destroyed || error) return;
            let enabled = result[0];
            this._conversationMode = enabled;
            this._updateModePill();
            // Sync the panel menu toggle (without re-triggering its callback)
            if (this._menuConversationToggle)
                this._menuConversationToggle.setToggleState(enabled);
        });
    }

    // A pill is a real button, not a painted label: St.Button carries key
    // focus, Enter/Space activation and an AT-SPI push-button role for free
    // — the same things a screen reader needs to see it at all. The child
    // label is explicit so text metrics and alignment stay exactly where the
    // St.Label version put them; the visual identity must not move.
    _createPill(text, styleClass, accessibleName, onActivate) {
        let label = new St.Label({
            text: text,
            style_class: 'gnome-speaks-pill-label',
            y_align: Clutter.ActorAlign.CENTER,
            x_align: Clutter.ActorAlign.CENTER,
        });
        let pill = new St.Button({
            style_class: styleClass,
            child: label,
            reactive: true,
            can_focus: true,
            track_hover: true,
            y_align: Clutter.ActorAlign.CENTER,
            x_align: Clutter.ActorAlign.CENTER,
            accessible_name: accessibleName,
        });
        pill._pillLabel = label;
        pill.connect('clicked', (actor, button) => {
            // St.Button reports button 0 for keyboard activation — the only
            // honest signal of "this came from the keyboard", which the
            // Chronicle needs so it only steals focus when a keyboard asked.
            onActivate(button === 0);
        });
        return pill;
    }

    _setPillState(pill, text, accessibleName) {
        if (!pill) return;
        if (pill._pillLabel)
            pill._pillLabel.text = text;
        pill.accessible_name = accessibleName;
    }

    _toggleContinuous() {
        if (!this._proxy) {
            this._continuousMode = !this._continuousMode;
            this._updateContinuousPill();
            return;
        }
        this._proxy.ToggleContinuousDictationRemote((result, error) => {
            if (this._destroyed || error) return;
            this._continuousMode = result[0];
            this._updateContinuousPill();
            if (this._menuContinuousToggle)
                this._menuContinuousToggle.setToggleState(this._continuousMode);
        });
    }

    _updateContinuousPill() {
        if (!this._continuousPill) return;
        let on = this._continuousMode;
        this._setPillState(this._continuousPill, on ? '🔄 Loop' : '🔄',
            on ? 'Continuous dictation: on — activate to stop looping'
                : 'Continuous dictation: off — activate to loop');
        this._continuousPill.remove_style_class_name(on ? 'gnome-speaks-pill-off' : 'gnome-speaks-pill-on');
        this._continuousPill.add_style_class_name(on ? 'gnome-speaks-pill-on' : 'gnome-speaks-pill-off');
    }

    _toggleTerminal() {
        if (!this._proxy) {
            this._terminalMode = !this._terminalMode;
            this._updateTerminalPill();
            return;
        }
        this._proxy.ToggleTerminalModeRemote((result, error) => {
            if (this._destroyed || error) return;
            this._terminalMode = result[0];
            this._updateTerminalPill();
        });
    }

    _updateTerminalPill() {
        if (!this._terminalPill) return;
        let on = this._terminalMode;
        this._setPillState(this._terminalPill, on ? '> Term' : '>',
            on ? 'Terminal mode: on — activate to turn off'
                : 'Terminal mode: off — activate to turn on');
        this._terminalPill.remove_style_class_name(on ? 'gnome-speaks-pill-off' : 'gnome-speaks-pill-on');
        this._terminalPill.add_style_class_name(on ? 'gnome-speaks-pill-on' : 'gnome-speaks-pill-off');
    }

    _showPills(visible) {
        if (!this._pills || !this._badge) return;

        // Capture badge center before width changes
        let oldWidth = this._badge.get_width();
        let centerX = this._badge.x + oldWidth / 2;

        for (let pill of this._pills) {
            if (visible)
                pill.show();
            else
                pill.hide();
        }

        // Re-center badge after pills change width
        GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            if (this._destroyed || !this._badge)
                return GLib.SOURCE_REMOVE;
            let newWidth = this._badge.get_width();
            if (newWidth !== oldWidth) {
                let newX = Math.round(centerX - newWidth / 2);
                this._badge.set_position(newX, this._badge.y);
                // Update saved position so re-centering persists
                if (this._customPosition)
                    this._customPosition.x = newX;
                this._positionWaveform();
                this._positionSubtitleOverlay();
            }
            return GLib.SOURCE_REMOVE;
        });
    }

    // -- Subtitle overlay --------------------------------------------------

    _createSubtitleOverlay() {
        this._subtitleOverlay = new St.BoxLayout({
            style_class: 'gnome-speaks-subtitle-overlay',
            ...boxOrientation(true),
            reactive: false,
            x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.END,
        });

        this._subtitleLabel = new St.Label({
            style_class: 'gnome-speaks-subtitle-text',
            text: '',
            x_align: Clutter.ActorAlign.CENTER,
        });

        // Single-line ticker: no wrapping — Pango START ellipsizing keeps
        // the TAIL of the transcript visible, so long dictation scrolls
        // past a leading "…" instead of wrapping into an ever-taller block.
        let clutterText = this._subtitleLabel.get_clutter_text();
        clutterText.set_line_wrap(false);
        clutterText.set_ellipsize(1); // PANGO_ELLIPSIZE_START

        this._subtitleOverlay.add_child(this._subtitleLabel);

        // Start hidden
        this._subtitleOverlay.opacity = 0;
        this._subtitleOverlay.hide();
        this._subtitleVisible = false;

        Main.uiGroup.add_child(this._subtitleOverlay);

        this._positionSubtitleOverlay();

        // Listen for settings changes
        if (this._settings) {
            let subtitleToggleId = this._settings.connect('changed::live-subtitles', () => {
                if (!this._settings.get_boolean('live-subtitles'))
                    this._hideSubtitle(true);
            });
            this._signals.push({obj: this._settings, id: subtitleToggleId});
        }
    }

    _applySubtitleColor() {
        if (!this._subtitleOverlay)
            return;

        // Remove existing color classes
        let colors = ['cream', 'gold', 'green', 'light_green', 'yellow', 'amber',
            'rust', 'red', 'light_red', 'blue', 'light_blue', 'cyan', 'light_cyan',
            'magenta', 'light_magenta', 'white', 'gray'];
        for (let c of colors)
            this._subtitleOverlay.remove_style_class_name(`gnome-speaks-subtitle-${c}`);

        // Pick color based on current state: user speech vs TTS
        let color;
        if (this._state === States.SPEAKING)
            color = this._getConfigFlag('subtitle_color_tts', 'amber');
        else
            color = this._getConfigFlag('subtitle_color_user', 'light_green');

        if (color && color !== 'default')
            this._subtitleOverlay.add_style_class_name(`gnome-speaks-subtitle-${color}`);
    }

    // The monitor the badge actually sits on — NOT primaryMonitor. Anything
    // that positions chrome against the badge must clamp to this, or on a
    // multi-head desk it gets yanked onto the primary display and reads as
    // "the button did nothing".
    _monitorForBadge() {
        if (!this._badge)
            return Main.layoutManager.primaryMonitor;
        let cx = this._badge.x + this._badge.width / 2;
        let cy = this._badge.y + this._badge.height / 2;
        for (let m of Main.layoutManager.monitors) {
            if (cx >= m.x && cx < m.x + m.width &&
                cy >= m.y && cy < m.y + m.height)
                return m;
        }
        return Main.layoutManager.primaryMonitor;
    }

    _positionSubtitleOverlay() {
        if (!this._subtitleOverlay || !this._badge)
            return;

        let badge = this._badge;
        let monitor = this._monitorForBadge();
        if (!monitor)
            return;

        let overlayWidth = Math.min(600, monitor.width - 80);
        this._subtitleOverlay.set_style(`max-width: ${overlayWidth}px; min-width: 200px;`);

        // Show above or below badge depending on position within THIS monitor
        let screenMidY = monitor.y + monitor.height / 2;
        let showAbove = (badge.y + badge.height / 2) > screenMidY;

        // Horizontal: center on badge, clamp to THIS monitor's edges
        let overlayX = badge.x + badge.width / 2 - overlayWidth / 2;
        overlayX = Math.max(monitor.x + 20, Math.min(overlayX, monitor.x + monitor.width - overlayWidth - 20));

        // Vertical: 12px gap above or below badge
        let overlayY;
        if (showAbove) {
            let estHeight = this._subtitleOverlay.height || 60;
            overlayY = badge.y - estHeight - 12;
        } else {
            overlayY = badge.y + badge.height + 12;
        }

        this._subtitleOverlay.set_position(overlayX, overlayY);
    }

    _showSubtitle(text, isPartial = false, useMarkup = false) {
        if (this._destroyed)
            return;

        // Respect live_subtitles setting
        if (this._settings && !this._settings.get_boolean('live-subtitles'))
            return;

        if (!this._subtitleOverlay || !this._subtitleLabel)
            return;

        // Apply per-state color (user speech vs TTS)
        this._applySubtitleColor();

        if (useMarkup) {
            // Pango markup mode — text is pre-formatted with word highlights
            this._subtitleLabel.clutter_text.set_markup(text);
            this._subtitleText = text;
        } else {
            // Plain text mode — sliding window for very long text
            let displayText = text;
            if (displayText.length > 300) {
                let start = displayText.length - 200;
                let spaceIdx = displayText.indexOf(' ', start);
                if (spaceIdx > 0 && spaceIdx < start + 30)
                    start = spaceIdx + 1;
                displayText = `...${displayText.substring(start)}`;
            }

            this._subtitleText = displayText;
            this._subtitleLabel.text = displayText;
        }

        // Apply partial/final style
        this._subtitleOverlay.remove_style_class_name('gnome-speaks-subtitle-partial');
        this._subtitleOverlay.remove_style_class_name('gnome-speaks-subtitle-final');
        this._subtitleOverlay.add_style_class_name(
            isPartial ? 'gnome-speaks-subtitle-partial' : 'gnome-speaks-subtitle-final');

        // Cancel any pending fade-out
        this._cancelTimeout('subtitle-fadeout');

        if (!this._subtitleVisible) {
            // Fade in
            this._subtitleOverlay.show();
            this._subtitleOverlay.remove_all_transitions();
            this._subtitleOverlay.ease({
                opacity: 255,
                duration: 300,
                mode: Clutter.AnimationMode.EASE_OUT_QUAD,
            });
            this._subtitleVisible = true;
        }

        // Reposition after text change (size may have changed)
        GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
            if (this._destroyed) return GLib.SOURCE_REMOVE;
            this._positionSubtitleOverlay();
            return GLib.SOURCE_REMOVE;
        });
    }

    _hideSubtitle(immediate = false) {
        if (!this._subtitleOverlay || !this._subtitleVisible)
            return;

        if (immediate) {
            this._subtitleOverlay.remove_all_transitions();
            this._subtitleOverlay.opacity = 0;
            this._subtitleOverlay.hide();
            this._subtitleVisible = false;
            this._subtitleText = '';
            return;
        }

        // Fade out over 500ms — no onComplete (GC safety)
        this._subtitleOverlay.remove_all_transitions();
        this._subtitleOverlay.ease({
            opacity: 0,
            duration: 500,
            mode: Clutter.AnimationMode.EASE_IN_QUAD,
        });
        let fadeId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 520, () => {
            this._cancelTimeout('subtitle-fade-finish');
            if (this._subtitleOverlay && !this._destroyed) {
                this._subtitleOverlay.hide();
                this._subtitleVisible = false;
                this._subtitleText = '';
            }
            return GLib.SOURCE_REMOVE;
        });
        this._trackTimeout(fadeId, 'subtitle-fade-finish');
    }

    _scheduleSubtitleFadeout(delayMs = 3000) {
        this._cancelTimeout('subtitle-fadeout');
        let timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delayMs, () => {
            if (!this._destroyed)
                this._hideSubtitle();
            this._removeTimeout('subtitle-fadeout');
            return GLib.SOURCE_REMOVE;
        });
        this._trackTimeout(timeoutId, 'subtitle-fadeout');
    }

    _destroySubtitleOverlay() {
        this._cancelTimeout('subtitle-fadeout');
        if (this._subtitleOverlay) {
            this._subtitleOverlay.remove_all_transitions();
            Main.uiGroup.remove_child(this._subtitleOverlay);
            this._subtitleOverlay.destroy();
            this._subtitleOverlay = null;
            this._subtitleLabel = null;
        }
        this._subtitleVisible = false;
        this._subtitleText = '';
    }

    _updateAudioInfoMenu(info) {
        if (!this._menuAudioInfoItem)
            return;
        let icon = info.device_type === 'headphones' ? '\u{1F3A7}' : '\u{1F50A}';
        let type = info.device_type === 'headphones' ? 'Headphones'
            : info.device_type === 'speakers' ? 'Speakers' : 'Unknown';
        let desc = info.description || '';
        let duplex = info.half_duplex ? 'half-duplex' : 'full-duplex';
        let ec = info.echo_cancel ? ', EC' : '';
        let label = `${icon} ${type}: ${duplex}${ec}`;
        if (desc)
            label = `${icon} ${desc} (${duplex}${ec})`;
        this._menuAudioInfoItem.label.text = label;
    }

    _updateModePill() {
        if (!this._modePill) return;
        let isChat = this._conversationMode;
        this._setPillState(this._modePill, isChat ? '🤖 AI' : '✏️ Type',
            isChat ? 'Mode: AI chat — activate to switch to typing'
                : 'Mode: typing — activate to switch to AI chat');
        this._modePill.remove_style_class_name(
            isChat ? 'gnome-speaks-mode-dict' : 'gnome-speaks-mode-chat');
        this._modePill.add_style_class_name(
            isChat ? 'gnome-speaks-mode-chat' : 'gnome-speaks-mode-dict');

        // Update badge border tint for chat mode
        if (this._badge) {
            if (isChat)
                this._badge.add_style_class_name('gnome-speaks-chat-active');
            else
                this._badge.remove_style_class_name('gnome-speaks-chat-active');
        }
    }

    _destroyBadge() {
        for (let sig of this._signals) {
            try {
                sig.obj.disconnect(sig.id);
            } catch (e) {
                // Already disconnected
            }
        }
        this._signals = [];

        this._destroyContextMenu();
        this._destroyChronicleScroll();

        if (this._vadDot) {
            this._vadDot.remove_all_transitions();
            Main.uiGroup.remove_child(this._vadDot);
            this._vadDot.destroy();
            this._vadDot = null;
        }

        if (this._waveformContainer) {
            this._waveformContainer.remove_all_transitions();
            Main.uiGroup.remove_child(this._waveformContainer);
            this._waveformContainer.destroy();
            this._waveformContainer = null;
            this._waveformRow = null;
            this._waveformRowTop = null;
            this._waveformBars = [];
            this._waveformBarsTop = [];
        }

        if (this._badge) {
            if (this._ctrlAltTabRegistered) {
                try {
                    Main.ctrlAltTabManager.removeGroup(this._badge);
                } catch (e) {
                    // Shell already dropped it (badge destroy does this too)
                }
                this._ctrlAltTabRegistered = false;
            }
            this._badge.remove_all_transitions();
            Main.layoutManager.removeChrome(this._badge);
            this._badge.destroy();
            this._badge = null;
        }
        this._icon = null;
        this._label = null;
        this._qualityPill = null;
        this._modePill = null;
        this._continuousPill = null;
        this._terminalPill = null;
        this._chroniclePill = null;
        this._pills = null;
    }

    _createPanelIndicator() {
        this._panelButton = new PanelMenu.Button(0.0, 'GNOME Speaks', false);

        this._panelIcon = new St.Icon({
            icon_name: 'audio-input-microphone-symbolic',
            style_class: 'system-status-icon gnome-speaks-panel-idle',
        });
        this._panelButton.add_child(this._panelIcon);

        // Build the dropdown menu
        this._buildPanelMenu();

        Main.panel.addToStatusArea('gnome-speaks', this._panelButton);
    }

    _buildPanelMenu() {
        let menu = this._panelButton.menu;

        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Actions'));

        this._menuListenItem = new PopupMenu.PopupMenuItem('Start Listening');
        this._menuListenItem.connect('activate', () => {
            if (this._state === States.LISTENING)
                this._callMethod('StopListening');
            else if (this._state === States.IDLE)
                this._callMethod('StartListening');
        });
        menu.addMenuItem(this._menuListenItem);

        this._menuStopItem = new PopupMenu.PopupMenuItem('Stop');
        this._menuStopItem.connect('activate', () => {
            this._callMethod('Stop');
        });
        menu.addMenuItem(this._menuStopItem);

        let speakClipItem = new PopupMenu.PopupMenuItem('Speak Clipboard');
        speakClipItem.connect('activate', () => {
            this._callMethod('SpeakClipboard');
        });
        menu.addMenuItem(speakClipItem);

        let readSelItem = new PopupMenu.PopupMenuItem('Read Selection');
        readSelItem.connect('activate', () => {
            this._callMethod('SpeakSelection');
        });
        menu.addMenuItem(readSelItem);

        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Mode'));

        this._menuConversationToggle = new PopupMenu.PopupSwitchMenuItem('AI Conversation', false);
        this._menuConversationToggle.connect('toggled', (item, state) => {
            if (!this._proxy) return;
            this._proxy.ToggleConversationModeRemote((result, error) => {
                if (this._destroyed || error) return;
                let enabled = result[0];
                this._conversationMode = enabled;
                this._updateModePill();
                if (enabled !== state)
                    item.setToggleState(enabled);
            });
        });
        menu.addMenuItem(this._menuConversationToggle);

        this._menuContinuousToggle = new PopupMenu.PopupSwitchMenuItem('Continuous Dictation', false);
        this._menuContinuousToggle.connect('toggled', () => {
            this._callMethod('ToggleContinuousDictation');
        });
        menu.addMenuItem(this._menuContinuousToggle);

        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Voice & Audio'));

        this._voiceQuality = 'fast';
        this._menuVoiceQualityItem = new PopupMenu.PopupMenuItem('Voice: HD');
        this._menuVoiceQualityItem.connect('activate', () => this._toggleVoiceQuality());
        menu.addMenuItem(this._menuVoiceQualityItem);

        this._menuAudioInfoItem = new PopupMenu.PopupMenuItem('Audio: detecting...');
        this._menuAudioInfoItem.setSensitive(false);
        menu.addMenuItem(this._menuAudioInfoItem);

        this._langSubMenu = new PopupMenu.PopupSubMenuMenuItem('Language: en-US');
        let languages = ['en-US', 'en-GB', 'en-AU', 'de-DE', 'fr-FR', 'es-ES', 'it-IT', 'ja-JP', 'ko-KR', 'zh-CN', 'pt-BR', 'ru-RU', 'ar-SA', 'hi-IN', 'nl-NL'];
        for (let lang of languages) {
            let item = new PopupMenu.PopupMenuItem(lang);
            item.connect('activate', () => {
                this._callMethod('SetLanguage', lang);
                this._langSubMenu.label.text = `Language: ${lang}`;
            });
            this._langSubMenu.menu.addMenuItem(item);
        }
        menu.addMenuItem(this._langSubMenu);

        // The Chronicle — everything said, newest first; click to respeak.
        this._chronicleSubMenu = new PopupMenu.PopupSubMenuMenuItem('Chronicle');
        // PopupSubMenu.open() refuses to open when empty (popupMenu.js:
        // `if (this.isEmpty()) return;`), so an empty submenu is a dead
        // row that can never fire open-state-changed to fill itself.
        // Seed a placeholder so it can always open, and refresh from the
        // PARENT menu's open — content is ready before the row is clicked.
        let seedItem = new PopupMenu.PopupMenuItem('Nothing recorded yet');
        seedItem.setSensitive(false);
        this._chronicleSubMenu.menu.addMenuItem(seedItem);
        menu.addMenuItem(this._chronicleSubMenu);
        let menuOpenId = menu.connect('open-state-changed', (_m, open) => {
            if (open && !this._destroyed)
                this._populateChronicleMenu();
        });
        this._signals.push({obj: menu, id: menuOpenId});

        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._menuBadgeToggle = new PopupMenu.PopupSwitchMenuItem('Show Badge', this._badgeVisible);
        this._menuBadgeToggle.connect('toggled', (item, state) => {
            this._badgeVisible = state;
            if (this._badge) {
                if (state) {
                    this._badge.show();
                    this._badge.ease({opacity: 255, duration: 200, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
                } else {
                    this._badge.ease({
                        opacity: 0, duration: 200, mode: Clutter.AnimationMode.EASE_OUT_QUAD,
                    });
                    GLib.timeout_add(GLib.PRIORITY_DEFAULT, 220, () => {
                        if (!this._destroyed && this._badge) this._badge.hide();
                        return GLib.SOURCE_REMOVE;
                    });
                }
            }
        });
        menu.addMenuItem(this._menuBadgeToggle);

        let settingsItem = new PopupMenu.PopupMenuItem('Preferences...');
        settingsItem.connect('activate', () => {
            this.openPreferences();
        });
        menu.addMenuItem(settingsItem);

        let disableItem = new PopupMenu.PopupMenuItem('Disable Extension');
        disableItem.connect('activate', () => {
            Main.extensionManager.disableExtension(this.uuid);
        });
        menu.addMenuItem(disableItem);
    }

    _populateChronicleMenu() {
        if (this._destroyed || !this._proxy || !this._chronicleSubMenu)
            return;

        this._proxy.GetChronicleRemote(12, (result, error) => {
            if (this._destroyed || !this._chronicleSubMenu)
                return;
            let submenu = this._chronicleSubMenu.menu;
            submenu.removeAll();

            let entries = [];
            if (!error && result) {
                try {
                    entries = JSON.parse(result[0]);
                } catch (e) {
                    entries = [];
                }
            }

            if (entries.length === 0) {
                let empty = new PopupMenu.PopupMenuItem(
                    error ? 'Chronicle unavailable' : 'Nothing recorded yet');
                empty.setSensitive(false);
                submenu.addMenuItem(empty);
                return;
            }

            // Newest first — the thing you want to hear again is recent.
            entries.reverse();
            for (let entry of entries) {
                let who = entry.kind === 'you' ? 'You' : 'It';
                let text = entry.text.replace(/\s+/g, ' ');
                if (text.length > 55)
                    text = `${text.substring(0, 55)}…`;
                let item = new PopupMenu.PopupMenuItem(`${who}: ${text}`);
                item.connect('activate', () => {
                    this._proxy.RespeakRemote(entry.id, () => {});
                });
                submenu.addMenuItem(item);
            }
        });
    }

    _updatePanelMenu() {
        if (!this._menuListenItem)
            return;

        switch (this._state) {
        case States.IDLE:
            this._menuListenItem.label.text = 'Start Listening';
            this._menuListenItem.setSensitive(true);
            this._menuStopItem.setSensitive(false);
            break;
        case States.LISTENING:
            this._menuListenItem.label.text = 'Stop Listening';
            this._menuListenItem.setSensitive(true);
            this._menuStopItem.setSensitive(true);
            break;
        case States.SPEAKING:
            this._menuListenItem.label.text = 'Start Listening';
            this._menuListenItem.setSensitive(false);
            this._menuStopItem.setSensitive(true);
            break;
        case States.PROCESSING:
            this._menuListenItem.label.text = 'Start Listening';
            this._menuListenItem.setSensitive(false);
            this._menuStopItem.setSensitive(false);
            break;
        }
    }

    _destroyPanelIndicator() {
        if (this._panelButton) {
            this._panelButton.destroy();
            this._panelButton = null;
            this._panelIcon = null;
            this._menuListenItem = null;
            this._menuStopItem = null;
            this._menuBadgeToggle = null;
            this._menuContinuousToggle = null;
            this._menuConversationToggle = null;
            this._menuVoiceQualityItem = null;
            this._menuAudioInfoItem = null;
            this._langSubMenu = null;
        }
    }

    _positionBadge() {
        if (!this._badge)
            return;

        let monitor = Main.layoutManager.primaryMonitor;

        if (this._customPosition) {
            // If saved position is within the current monitor, use it.
            // Otherwise reset to default center-bottom (the user moved it
            // on a different display config).
            if (monitor) {
                let cx = this._customPosition.x;
                let cy = this._customPosition.y;
                let inBounds = cx >= monitor.x && cy >= monitor.y
                    && cx < monitor.x + monitor.width - 20
                    && cy < monitor.y + monitor.height - 20;
                if (inBounds) {
                    this._badge.set_position(cx, cy);
                    this._positionWaveform();
                    this._positionSubtitleOverlay();
                    return;
                }
                // Out of bounds — fall through to default positioning
                this._customPosition = null;
                if (this._settings) {
                    this._settings.set_int('badge-position-x', -1);
                    this._settings.set_int('badge-position-y', -1);
                }
            } else {
                this._badge.set_position(this._customPosition.x, this._customPosition.y);
                this._positionWaveform();
                this._positionSubtitleOverlay();
                return;
            }
        }

        if (!monitor)
            return;

        // We need to wait for the badge to be allocated to get its width
        // Use a small delay to ensure the actor is laid out
        let timeoutId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            if (this._destroyed || !this._badge)
                return GLib.SOURCE_REMOVE;

            let badgeWidth = this._badge.get_width();
            let badgeHeight = this._badge.get_height();

            if (badgeWidth <= 0)
                badgeWidth = 48;
            if (badgeHeight <= 0)
                badgeHeight = 48;

            let x = monitor.x + Math.round((monitor.width - badgeWidth) / 2);
            let y = monitor.y + monitor.height - 80;

            this._badge.set_position(x, y);
            this._positionWaveform();
            this._positionSubtitleOverlay();
            return GLib.SOURCE_REMOVE;
        });
        this._trackTimeout(timeoutId);
    }

    _positionWaveform() {
        if (!this._waveformContainer || !this._badge) return;
        let badge = this._badge;
        let waveWidth = badge.width;
        this._waveformContainer.set_width(waveWidth);
        this._waveformContainer.set_position(
            badge.x,
            badge.y + badge.height + 4
        );
        // VAD dot: right side of badge, vertically centered
        if (this._vadDot) {
            this._vadDot.set_position(
                badge.x + badge.width + 8,
                badge.y + badge.height / 2 - 6
            );
        }
    }

    _connectLayoutSignals() {
        let monitorId = Main.layoutManager.connect('monitors-changed', () => {
            this._customPosition = null;
            if (this._settings) {
                this._settings.set_int('badge-position-x', -1);
                this._settings.set_int('badge-position-y', -1);
            }
            this._positionBadge();
            this._positionSubtitleOverlay();
        });
        this._layoutSignalId = monitorId;
    }

    _disconnectLayoutSignals() {
        if (this._layoutSignalId) {
            Main.layoutManager.disconnect(this._layoutSignalId);
            this._layoutSignalId = null;
        }
    }

    _initProxy() {
        if (this._proxyPending) return;
        this._proxyPending = true;
        try {
            let p = new GnomeSpeaksProxy(
                Gio.DBus.session,
                DBUS_NAME,
                DBUS_PATH,
                (proxy, error) => {
                    this._proxyPending = false;
                    if (this._destroyed) return;
                    if (error) {
                        console.warn(`[GNOME Speaks] DBus proxy creation failed: ${error.message}`);
                        this._proxy = null;
                        this._proxyReady = false;
                        return;
                    }
                    this._proxy = p;
                    this._proxyReady = true;
                    this._connectProxySignals();
                    this._syncState();
                }
            );
        } catch (e) {
            console.warn(`[GNOME Speaks] Failed to create DBus proxy: ${e.message}`);
            this._proxyPending = false;
            this._proxy = null;
            this._proxyReady = false;
        }
    }

    _connectProxySignals() {
        if (!this._proxy)
            return;

        let stateChangedId = this._proxy.connectSignal('StateChanged', (proxy, sender, [state]) => {
            if (this._destroyed) return;
            this._setState(state);
        });
        this._proxySignals.push(stateChangedId);

        let transcriptionId = this._proxy.connectSignal('TranscriptionReady', (proxy, sender, [text]) => {
            if (this._destroyed) return;
            this._showTranscription(text);
        });
        this._proxySignals.push(transcriptionId);

        let partialId = this._proxy.connectSignal('PartialTranscription', (proxy, sender, [text]) => {
            if (this._destroyed) return;
            this._showPartialTranscription(text);
        });
        this._proxySignals.push(partialId);

        let subtitleUpdateId = this._proxy.connectSignal('SubtitleUpdate', (proxy, sender, [text, duration, percent]) => {
            if (this._destroyed) return;
            this._onSubtitleUpdate(text, duration, percent);
        });
        this._proxySignals.push(subtitleUpdateId);

        let audioLevelId = this._proxy.connectSignal('AudioLevel', (proxy, sender, [level]) => {
            if (this._destroyed) return;
            this._onAudioLevel(level);
        });
        this._proxySignals.push(audioLevelId);

        let sttStatusId = this._proxy.connectSignal('STTStatus', (proxy, sender, [speechDetected, timeoutFraction]) => {
            if (this._destroyed) return;
            this._onSTTStatus(speechDetected, timeoutFraction);
        });
        this._proxySignals.push(sttStatusId);

        let errorId = this._proxy.connectSignal('Error', (proxy, sender, [message]) => {
            if (this._destroyed) return;
            this._showError(message);
        });
        this._proxySignals.push(errorId);
    }

    _disconnectProxy() {
        this._proxyReady = false;
        this._proxyPending = false;
        if (this._proxy) {
            for (let sigId of this._proxySignals) {
                try {
                    this._proxy.disconnectSignal(sigId);
                } catch (e) {
                    // Already disconnected
                }
            }
            this._proxySignals = [];
            // Prevent proxy from being GC'd during disable() — defer the
            // reference drop to the next idle cycle so GjsDBusImplementation
            // finalization doesn't trigger JS callbacks during the GC sweep.
            let oldProxy = this._proxy;
            this._proxy = null;
            GLib.idle_add(GLib.PRIORITY_LOW, () => {
                void oldProxy;
                return GLib.SOURCE_REMOVE;
            });
        }
    }

    _syncState() {
        if (!this._proxy || this._destroyed)
            return;

        this._proxy.GetStateRemote((result, error) => {
            if (this._destroyed || !this._proxy) return;
            if (error) {
                console.debug(`[GNOME Speaks] GetState failed: ${error.message}`);
                return;
            }
            if (result && result[0])
                this._setState(result[0]);
        });

        // Sync voice quality pill + menu label
        this._proxy.GetVoiceQualityRemote((result, error) => {
            if (this._destroyed || !this._proxy) return;
            if (!error && result && result[0]) {
                this._voiceQuality = result[0];
                this._updateQualityPill();
                if (this._menuVoiceQualityItem)
                    this._menuVoiceQualityItem.label.text = result[0] === 'hd'
                        ? 'Voice: HD' : 'Voice: Fast';
            }
        });

        // Sync audio device info
        this._proxy.GetAudioInfoRemote((result, error) => {
            if (this._destroyed || !this._proxy) return;
            if (!error && result && result[0]) {
                try {
                    let info = JSON.parse(result[0]);
                    this._updateAudioInfoMenu(info);
                } catch (e) {
                    // Ignore parse errors
                }
            }
        });

        // Sync mode states from service
        this._proxy.GetConversationModeRemote((result, error) => {
            if (this._destroyed || !this._proxy) return;
            if (!error && result) {
                this._conversationMode = result[0];
                this._updateModePill();
                if (this._menuConversationToggle)
                    this._menuConversationToggle.setToggleState(this._conversationMode);
            }
        });
        this._proxy.GetContinuousDictationRemote((result, error) => {
            if (this._destroyed || !this._proxy) return;
            if (!error && result) {
                this._continuousMode = result[0];
                this._updateContinuousPill();
                if (this._menuContinuousToggle)
                    this._menuContinuousToggle.setToggleState(this._continuousMode);
            }
        });
        this._proxy.GetTerminalModeRemote((result, error) => {
            if (this._destroyed || !this._proxy) return;
            if (!error && result) {
                this._terminalMode = result[0];
                this._updateTerminalPill();
            }
        });
    }

    _setState(newState) {
        if (this._destroyed) return;
        if (!Object.values(States).includes(newState))
            return;
        if (this._state === newState)
            return;

        let oldState = this._state;
        this._state = newState;

        if (!this._badge)
            return;

        let config = STATE_CONFIG[newState];

        // Update style classes — only remove old, add new (avoids 4 remove calls)
        if (oldState && STATE_CONFIG[oldState]) {
            this._badge.remove_style_class_name(STATE_CONFIG[oldState].styleClass);
        }
        this._badge.remove_style_class_name('gnome-speaks-error');
        this._badge.add_style_class_name(config.styleClass);
        this._badge.accessible_name = config.accessibleName;

        // Update waveform bar color for state
        if (this._waveformBars) {
            let barClass = {
                [States.LISTENING]: 'gnome-speaks-waveform-bar-listening',
                [States.SPEAKING]: 'gnome-speaks-waveform-bar-speaking',
            }[newState];
            for (let bar of this._waveformBars) {
                bar.remove_style_class_name('gnome-speaks-waveform-bar-listening');
                bar.remove_style_class_name('gnome-speaks-waveform-bar-speaking');
                if (barClass) bar.add_style_class_name(barClass);
            }
        }

        // Update icon
        if (this._icon) {
            this._icon.icon_name = config.iconName;
            this._icon.x_expand = (newState === States.IDLE);
        }

        // Update label
        if (this._label) {
            if (config.showLabel) {
                this._label.text = config.label;
                this._label.show();
            } else {
                this._label.text = '';
                this._label.hide();
            }
        }

        // Pills: hidden in idle, shown when active
        let showPills = newState !== States.IDLE;
        let wasShowingPills = oldState && oldState !== States.IDLE;
        if (showPills !== wasShowingPills) {
            this._showPills(showPills);
            // Only refresh pill content when transitioning visibility
            if (showPills) {
                this._updateQualityPill();
                this._updateModePill();
                this._updateContinuousPill();
                this._updateTerminalPill();
            }
        }

        // Update panel icon and menu
        this._updatePanelIcon(newState);
        this._updatePanelMenu();

        // Re-sync mode flags when returning to idle (catches one-shot AI mode).
        // Debounce: skip if we just synced < 2s ago (avoids D-Bus flood in AI+Loop)
        if (newState === States.IDLE && this._proxy) {
            let now = GLib.get_monotonic_time();
            if (!this._lastModeSyncTime || (now - this._lastModeSyncTime) > 2000000) {
                this._lastModeSyncTime = now;
                this._proxy.GetConversationModeRemote((result, error) => {
                    if (this._destroyed || !this._proxy) return;
                    if (!error && result) {
                        this._conversationMode = result[0];
                        this._updateModePill();
                        if (this._menuConversationToggle)
                            this._menuConversationToggle.setToggleState(this._conversationMode);
                    }
                });
            }
        }

        // Reset audio level when leaving listening state
        if (newState !== States.LISTENING)
            this._audioLevel = 0;

        // Reset word tracking for new utterance
        if (newState === States.IDLE || newState === States.LISTENING) {
            this._previousWords = [];
            this._wordHighlights = [];
            this._cancelTimeout('word-highlight-fade');
        }

        // Hide VAD dot when leaving listening
        if (newState !== States.LISTENING && this._vadDot) {
            this._vadDot.remove_all_transitions();
            this._vadDot.opacity = 0;
        }

        // Fade waveform when leaving active states
        if (newState === States.IDLE && this._waveformContainer) {
            this._waveformContainer.ease({
                opacity: 0, duration: 400,
                mode: Clutter.AnimationMode.EASE_OUT_QUAD,
            });
            this._waveformLevels.fill(0);
        }

        // Handle animations
        if (newState === States.LISTENING || newState === States.SPEAKING) {
            this._startPulse();
        } else {
            this._stopPulse();
        }

        // Subtitle overlay state management:
        // - IDLE: schedule a graceful 3s fade-out (don't hide immediately)
        // - PROCESSING: keep subtitles visible (transcription just finished)
        // - LISTENING/SPEAKING: subtitles managed by their signal handlers
        if (newState === States.IDLE) {
            // Don't immediately hide — schedule a gentle fade-out
            if (this._subtitleVisible)
                this._scheduleSubtitleFadeout(3000);
        } else if (newState === States.PROCESSING) {
            // Keep transcription visible while processing
            this._cancelTimeout('subtitle-fadeout');
        }

        // Reposition badge + glow + subtitle if going to/from idle (size changes)
        if ((oldState === States.IDLE && newState !== States.IDLE) ||
            (oldState !== States.IDLE && newState === States.IDLE)) {
            if (!this._customPosition) {
                let delay = (newState === States.IDLE) ? 16 : 50;
                let timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delay, () => {
                    if (this._destroyed) return GLib.SOURCE_REMOVE;
                    this._positionBadge();
                    return GLib.SOURCE_REMOVE;
                });
                this._trackTimeout(timeoutId);
            } else {
                // Custom position — still need to reposition glow and subtitle
                GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
                    if (this._destroyed) return GLib.SOURCE_REMOVE;
                    this._positionWaveform();
                    this._positionSubtitleOverlay();
                    return GLib.SOURCE_REMOVE;
                });
            }
        }
    }

    _updatePanelIcon(state) {
        if (!this._panelIcon)
            return;

        let panelClasses = [
            'gnome-speaks-panel-idle',
            'gnome-speaks-panel-listening',
            'gnome-speaks-panel-processing',
            'gnome-speaks-panel-speaking',
        ];

        for (let cls of panelClasses)
            this._panelIcon.remove_style_class_name(cls);

        let config = STATE_CONFIG[state];
        this._panelIcon.icon_name = config.iconName;
        this._panelIcon.add_style_class_name(`gnome-speaks-panel-${state}`);
    }

    _startPulse() {
        this._stopPulse();

        if (!this._badge)
            return;
        if (!this._getConfigFlag('show_badge_pulse', true))
            return;

        this._pulseUp = true;
        this._pulseActive = true;
        this._doPulse();
    }

    _doPulse() {
        if (this._destroyed || !this._badge || !this._pulseActive)
            return;

        let targetScale = this._pulseUp ? 1.06 : 1.0;

        // No onComplete — GC can invoke Clutter transition callbacks during
        // sweep, causing "JS callback during garbage collection" and black screen.
        // Use a GLib timeout instead; GLib sources are not actor-bound.
        this._badge.ease({
            scale_x: targetScale,
            scale_y: targetScale,
            duration: 800,
            mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
        });
        this._pulseNextId = GLib.timeout_add(GLib.PRIORITY_LOW, 820, () => {
            this._pulseNextId = null;
            if (this._destroyed || !this._pulseActive || !this._badge)
                return GLib.SOURCE_REMOVE;
            this._pulseUp = !this._pulseUp;
            this._doPulse();
            return GLib.SOURCE_REMOVE;
        });
    }

    _stopPulse() {
        this._pulseActive = false;

        if (this._pulseNextId) {
            try { GLib.Source.remove(this._pulseNextId); } catch (e) { /* already fired */ }
            this._pulseNextId = null;
        }

        // Cancel any pending audio-level pulse resume
        this._cancelTimeout('audio-pulse-resume');

        if (!this._badge || this._destroyed)
            return;

        this._badge.remove_all_transitions();

        this._badge.ease({
            scale_x: 1.0,
            scale_y: 1.0,
            duration: 200,
            mode: Clutter.AnimationMode.EASE_OUT_QUAD,
        });
    }

    _onBadgeClicked() {
        switch (this._state) {
        case States.IDLE:
            this._callMethod('StartListening');
            break;
        case States.LISTENING:
            this._callMethod('StopListening');
            break;
        case States.SPEAKING:
            this._callMethod('Stop');
            break;
        case States.PROCESSING:
            this._callMethod('Stop');
            break;
        }
    }

    _callMethod(methodName, ...args) {
        if (this._destroyed) return;
        if (!this._proxy) {
            this._initProxy();
            let timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
                if (this._destroyed || !this._proxy)
                    return GLib.SOURCE_REMOVE;
                this._callMethodInternal(methodName, ...args);
                return GLib.SOURCE_REMOVE;
            });
            this._trackTimeout(timeoutId);
            return;
        }

        this._callMethodInternal(methodName, ...args);
    }

    _callMethodInternal(methodName, ...args) {
        if (this._destroyed || !this._proxy)
            return;

        let remoteName = `${methodName}Remote`;
        if (typeof this._proxy[remoteName] !== 'function') {
            console.warn(`[GNOME Speaks] Unknown method: ${methodName}`);
            return;
        }

        let callback = (result, error) => {
            if (this._destroyed) return;
            if (error) {
                console.debug(`[GNOME Speaks] ${methodName} failed: ${error.message}`);
                this._showError(`${methodName} failed`);
            }
        };

        if (args.length > 0)
            this._proxy[remoteName](...args, callback);
        else
            this._proxy[remoteName](callback);
    }

    _buildWordMarkup(newText) {
        let newWords = newText.trim().split(/\s+/).filter(w => w.length > 0);
        let prevWords = this._previousWords || [];
        let now = GLib.get_monotonic_time();

        // Find common prefix length
        let commonLen = 0;
        while (commonLen < prevWords.length && commonLen < newWords.length
               && prevWords[commonLen] === newWords[commonLen]) {
            commonLen++;
        }

        let markupParts = [];
        for (let i = 0; i < newWords.length; i++) {
            let word = GLib.markup_escape_text(newWords[i], -1);
            if (i < commonLen) {
                // Settled word — check for fading highlight
                let highlight = this._wordHighlights.find(h => h.index === i);
                if (highlight && (now - highlight.time) < 600000) {
                    let color = highlight.type === 'new' ? WORD_COLOR_NEW : WORD_COLOR_CORRECTED;
                    markupParts.push(`<span foreground="${color}" font_weight="bold">${word}</span>`);
                } else {
                    markupParts.push(`<span foreground="${WORD_COLOR_SETTLED}">${word}</span>`);
                }
            } else if (i >= prevWords.length) {
                // Brand new word (appended)
                markupParts.push(`<span foreground="${WORD_COLOR_NEW}" font_weight="bold">${word}</span>`);
                this._wordHighlights.push({index: i, type: 'new', time: now});
            } else {
                // Corrected word (different from previous at this position)
                markupParts.push(`<span foreground="${WORD_COLOR_CORRECTED}" font_weight="bold">${word}</span>`);
                this._wordHighlights.push({index: i, type: 'corrected', time: now});
            }
        }

        // Clean up old highlights
        this._wordHighlights = this._wordHighlights.filter(h => (now - h.time) < 600000);

        this._previousWords = newWords;
        return markupParts.join(' ');
    }

    _showPartialTranscription(text) {
        if (this._destroyed)
            return;
        if (this._state !== States.LISTENING && this._state !== States.SPEAKING)
            return;
        // In dictation mode (not AI), text is typed at cursor — subtitles are redundant
        if (!this._conversationMode)
            return;
        // Per-source toggles. This path carries BOTH voices: your live STT
        // partials while LISTENING, and its streamed sentences while
        // SPEAKING — the state picks whose toggle applies (same convention
        // as _applySubtitleColor).
        const sourceFlag = this._state === States.SPEAKING
            ? 'subtitles_tts' : 'subtitles_user';
        if (!this._getConfigFlag(sourceFlag, true))
            return;

        if (text === this._lastPartialText)
            return;
        this._lastPartialText = text;

        // Build word-level markup or plain text depending on toggle
        let useMarkup = this._getConfigFlag('show_word_highlights', true);
        let displayContent = useMarkup ? this._buildWordMarkup(text) : text;
        this._pendingPartialMarkup = displayContent;

        if (this._partialDebounceId)
            return;

        // First update — render immediately
        this._showSubtitle(displayContent, true, useMarkup);
        this._lastPartialTime = GLib.get_monotonic_time();

        // Schedule gate for coalescing (200ms for snappier word-by-word feel)
        let timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 200, () => {
            this._partialDebounceId = null;
            this._removeTimeout('partial-debounce');
            if (this._destroyed)
                return GLib.SOURCE_REMOVE;
            if (this._pendingPartialMarkup) {
                let um = this._getConfigFlag('show_word_highlights', true);
                let fresh = um ? this._buildWordMarkup(this._lastPartialText) : this._lastPartialText;
                this._showSubtitle(fresh, true, um);
            }
            return GLib.SOURCE_REMOVE;
        });
        this._partialDebounceId = timeoutId;
        this._trackTimeout(timeoutId, 'partial-debounce');

        // Schedule highlight fade refresh after 650ms (only if word highlights on)
        if (useMarkup) {
            this._cancelTimeout('word-highlight-fade');
            let fadeId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 650, () => {
                this._removeTimeout('word-highlight-fade');
                if (this._destroyed) return GLib.SOURCE_REMOVE;
                if (this._lastPartialText && this._state === States.LISTENING) {
                    let freshMarkup = this._buildWordMarkup(this._lastPartialText);
                    this._showSubtitle(freshMarkup, true, true);
                }
                return GLib.SOURCE_REMOVE;
            });
            this._trackTimeout(fadeId, 'word-highlight-fade');
        }
    }

    _onSubtitleUpdate(text, duration, percent) {
        if (this._destroyed)
            return;
        if (this._state !== States.SPEAKING)
            return;
        // Per-source toggle: captions of ITS voice
        if (!this._getConfigFlag('subtitles_tts', true))
            return;

        // When reveal reaches 100%, always render immediately (final state)
        if (percent >= 100) {
            this._cancelTimeout('subtitle-debounce');
            this._showSubtitle(text, false);
            this._scheduleSubtitleFadeout(2000);
            return;
        }

        // Progressive reveal: show text up to the current estimated position
        let charPos = Math.floor((percent / 100.0) * text.length);
        let revealed = text.substring(0, charPos);

        if (revealed.length === 0)
            return;

        // Throttle progressive reveals to ~3/sec (330ms)
        let now = GLib.get_monotonic_time();
        if (this._lastSubtitleUpdateTime && (now - this._lastSubtitleUpdateTime) < 330000) {
            // Buffer the latest reveal text for the next scheduled flush
            this._pendingSubtitleReveal = revealed;
            if (!this._subtitleDebounceId) {
                let remaining = 330 - Math.floor((now - this._lastSubtitleUpdateTime) / 1000);
                let timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, Math.max(remaining, 16), () => {
                    this._subtitleDebounceId = null;
                    this._removeTimeout('subtitle-debounce');
                    if (this._destroyed || !this._pendingSubtitleReveal)
                        return GLib.SOURCE_REMOVE;
                    this._showSubtitle(this._pendingSubtitleReveal, true);
                    this._lastSubtitleUpdateTime = GLib.get_monotonic_time();
                    this._pendingSubtitleReveal = null;
                    return GLib.SOURCE_REMOVE;
                });
                this._subtitleDebounceId = timeoutId;
                this._trackTimeout(timeoutId, 'subtitle-debounce');
            }
            return;
        }

        this._lastSubtitleUpdateTime = now;
        this._showSubtitle(revealed, true);
    }

    _showTranscription(text) {
        if (this._destroyed)
            return;

        // Reset word tracking — partial phase is done
        this._previousWords = [];
        this._wordHighlights = [];
        this._cancelTimeout('word-highlight-fade');

        // In dictation mode, text is already at cursor — skip subtitle
        if (!this._conversationMode)
            return;
        // Per-source toggle: live transcript of YOUR voice
        if (!this._getConfigFlag('subtitles_user', true))
            return;

        // Show in the subtitle overlay — final, non-italic style
        this._showSubtitle(text, false);

        // Schedule fade-out after 3 seconds
        this._scheduleSubtitleFadeout(3000);
    }

    _showError(message) {
        console.warn(`[GNOME Speaks] Error: ${message}`);

        if (this._destroyed || !this._badge)
            return;

        this._badge.add_style_class_name('gnome-speaks-error');

        this._cancelTimeout('error');

        let timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 2000, () => {
            if (!this._destroyed && this._badge)
                this._badge.remove_style_class_name('gnome-speaks-error');
            this._removeTimeout('error');
            return GLib.SOURCE_REMOVE;
        });
        this._trackTimeout(timeoutId, 'error');
    }

    // -- STT status: VAD indicator + timeout progress ----------------------

    _onSTTStatus(speechDetected, timeoutFraction) {
        if (this._destroyed || !this._badge) return;
        if (this._state !== States.LISTENING) return;

        // VAD: toggle style class on badge for speech-detected glow
        if (speechDetected && !this._badge.has_style_class_name('gnome-speaks-vad-active')) {
            this._badge.add_style_class_name('gnome-speaks-vad-active');
        } else if (!speechDetected && this._badge.has_style_class_name('gnome-speaks-vad-active')) {
            this._badge.remove_style_class_name('gnome-speaks-vad-active');
        }

        // (Timeout visualization now handled by waveform fade in _onAudioLevel)
    }

    // -- Audio level visualization -----------------------------------------

    _onAudioLevel(level) {
        if (this._destroyed) return;
        this._audioLevel = level;
        if (!this._badge || (this._state !== States.LISTENING && this._state !== States.SPEAKING))
            return;

        // Throttle to ~12fps to avoid flooding the animation system
        let now = GLib.get_monotonic_time();
        if ((now - this._lastAudioLevelTime) < 80000) // 80ms
            return;
        this._lastAudioLevelTime = now;

        // Suspend pulse animation while audio levels drive the scale,
        // otherwise ease() and set_scale() fight over the same property
        // causing rapid jiggling.
        if (this._pulseActive) {
            this._pulseActive = false;
            if (this._pulseNextId) {
                try { GLib.Source.remove(this._pulseNextId); } catch (e) { /* already fired */ }
                this._pulseNextId = null;
            }
            this._badge.remove_all_transitions();
        }

        let clampedLevel = Math.min(level, 1.0);

        // Badge scale: 1.0 to 1.08 (subtle breathing)
        if (this._getConfigFlag('show_badge_scale', true)) {
            let scale = 1.0 + clampedLevel * 0.08;
            this._badge.set_scale(scale, scale);
        }

        // VAD indicator — separate glowing dot (respects toggle)
        if (this._vadDot && this._getConfigFlag('show_vad_dot', true)) {
            if (clampedLevel > 0.02) {
                this._lastVadTime = now;
                if (this._vadDot.opacity < 200) {
                    this._vadDot.remove_all_transitions();
                    this._vadDot.opacity = 255;
                }
            } else if (this._lastVadTime && (now - this._lastVadTime) > 1000000) {
                if (this._vadDot.opacity > 0) {
                    this._vadDot.ease({
                        opacity: 0, duration: 500,
                        mode: Clutter.AnimationMode.EASE_OUT_QUAD,
                    });
                }
            }
        }

        // Waveform bars: shift buffer and update heights (respects toggle)
        if (this._waveformContainer && this._waveformBars.length > 0
            && this._getConfigFlag('show_waveform', true)) {
            // Show waveform when audio arrives — reposition every frame
            // to track badge width changes (pills expanding/collapsing)
            this._positionWaveform();
            if (this._waveformContainer.opacity === 0) {
                this._waveformContainer.show();
                this._waveformContainer.ease({
                    opacity: 255, duration: 200,
                    mode: Clutter.AnimationMode.EASE_OUT_QUAD,
                });
            }
            // Shift levels left, add new sample at end
            this._waveformLevels.shift();
            this._waveformLevels.push(clampedLevel);
            // Track last speech-level audio for silence fade
            if (clampedLevel > 0.02)
                this._lastSpeechTime = now;

            // Silence fade: dim the waveform over 10s of silence
            let silenceMs = this._lastSpeechTime ? (now - this._lastSpeechTime) / 1000 : 0;
            let fadeFactor = (this._getConfigFlag('show_silence_fade', true) && silenceMs > 2000)
                ? Math.max(0.15, 1.0 - (silenceMs - 2000) / 10000)
                : 1.0;

            // Set bar heights (mirrored top + bottom) with log scaling + color
            for (let i = 0; i < this._waveformBars.length; i++) {
                let lvl = this._waveformLevels[i];
                let logLevel = Math.log(1 + lvl * 20) / Math.log(21);
                let h = Math.max(2, Math.floor(logLevel * 22 * fadeFactor));
                let barBot = this._waveformBars[i];
                let barTop = this._waveformBarsTop[i];
                barBot.set_height(h);
                barTop.set_height(h);
                // Three-color: green (good), amber (hot), red (clipped)
                // When fading, shift to dim gray
                let colorClass;
                if (fadeFactor < 0.5) {
                    colorClass = lvl > 0.02 ? 'gnome-speaks-waveform-bar-dim' : null;
                } else {
                    colorClass = lvl > 0.8 ? 'gnome-speaks-waveform-bar-clip'
                        : lvl > 0.35 ? 'gnome-speaks-waveform-bar-hot'
                        : lvl > 0.02 ? 'gnome-speaks-waveform-bar-good' : null;
                }
                for (let bar of [barBot, barTop]) {
                    bar.remove_style_class_name('gnome-speaks-waveform-bar-good');
                    bar.remove_style_class_name('gnome-speaks-waveform-bar-hot');
                    bar.remove_style_class_name('gnome-speaks-waveform-bar-clip');
                    bar.remove_style_class_name('gnome-speaks-waveform-bar-dim');
                    if (colorClass) bar.add_style_class_name(colorClass);
                }
            }
        }

        // Resume pulse + fade waveform after 800ms of silence
        this._cancelTimeout('audio-pulse-resume');
        let timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 800, () => {
            this._removeTimeout('audio-pulse-resume');
            if (this._destroyed || !this._badge) return GLib.SOURCE_REMOVE;
            // Fade waveform out
            if (this._waveformContainer) {
                this._waveformContainer.ease({
                    opacity: 0, duration: 400,
                    mode: Clutter.AnimationMode.EASE_OUT_QUAD,
                });
                // Reset levels
                this._waveformLevels.fill(0);
            }
            if (this._state === States.LISTENING || this._state === States.SPEAKING)
                this._startPulse();
            return GLib.SOURCE_REMOVE;
        });
        this._trackTimeout(timeoutId, 'audio-pulse-resume');
    }

    _showContextMenu(fromKeyboard = false) {
        if (!this._badge) return;
        this._destroyContextMenu();

        this._contextFocusReturn = fromKeyboard ? this._badge : null;
        this._contextMenuItems = [];

        this._contextMenu = new St.BoxLayout({
            ...boxOrientation(true),
            style_class: 'popup-menu-content',
            style: 'background-color: rgba(40,40,40,0.95); border-radius: 12px; padding: 8px 0; min-width: 200px;',
            reactive: true,
            can_focus: true,
            accessible_role: Atk.Role.MENU,
            accessible_name: 'GNOME Speaks actions',
        });

        this._contextMenu.connect('key-press-event', (actor, keyEvent) => {
            let symbol = keyEvent.get_key_symbol();
            if (symbol === Clutter.KEY_Escape) {
                this._destroyContextMenu();
                return Clutter.EVENT_STOP;
            }
            if (symbol === Clutter.KEY_Down || symbol === Clutter.KEY_Tab)
                return this._moveContextMenuFocus(1);
            if (symbol === Clutter.KEY_Up || symbol === Clutter.KEY_ISO_Left_Tab)
                return this._moveContextMenuFocus(-1);
            return Clutter.EVENT_PROPAGATE;
        });

        let titleItem = new St.Label({
            text: 'GNOME Speaks',
            style: 'padding: 8px 16px; font-weight: bold; color: rgba(255,255,255,0.5); font-size: 12px;',
        });
        this._contextMenu.add_child(titleItem);

        let separator = new St.Widget({
            style: 'height: 1px; background-color: rgba(255,255,255,0.1); margin: 4px 8px;',
        });
        this._contextMenu.add_child(separator);

        let speakClipboardItem = this._createMenuItem('Speak Clipboard', () => {
            this._callMethod('SpeakClipboard');
            this._destroyContextMenu();
        });
        this._contextMenu.add_child(speakClipboardItem);

        let readSelItem = this._createMenuItem('Read Selection', () => {
            this._callMethod('SpeakSelection');
            this._destroyContextMenu();
        });
        this._contextMenu.add_child(readSelItem);

        let stopItem = this._createMenuItem('Stop', () => {
            this._callMethod('Stop');
            this._destroyContextMenu();
        });
        this._contextMenu.add_child(stopItem);

        let separator2 = new St.Widget({
            style: 'height: 1px; background-color: rgba(255,255,255,0.1); margin: 4px 8px;',
        });
        this._contextMenu.add_child(separator2);

        let hideItem = this._createMenuItem('Hide Badge', () => {
            this._badgeVisible = false;
            if (this._menuBadgeToggle)
                this._menuBadgeToggle.setToggleState(false);
            this._badge.ease({
                opacity: 0, duration: 200, mode: Clutter.AnimationMode.EASE_OUT_QUAD,
            });
            GLib.timeout_add(GLib.PRIORITY_DEFAULT, 220, () => {
                if (!this._destroyed && this._badge) this._badge.hide();
                return GLib.SOURCE_REMOVE;
            });
            this._destroyContextMenu();
        });
        this._contextMenu.add_child(hideItem);

        Main.layoutManager.addTopChrome(this._contextMenu);

        if (this._contextFocusReturn && this._contextMenuItems.length > 0)
            this._contextMenuItems[0].grab_key_focus();

        // Position menu above the badge
        let [badgeX, badgeY] = this._badge.get_transformed_position();
        let badgeWidth = this._badge.get_width();

        // Wait for menu to be allocated so we know its size
        let posId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            if (!this._contextMenu)
                return GLib.SOURCE_REMOVE;

            let menuWidth = this._contextMenu.get_width();
            let menuHeight = this._contextMenu.get_height();

            let menuX = badgeX + (badgeWidth - menuWidth) / 2;
            let menuY = badgeY - menuHeight - 8;

            let monitor = Main.layoutManager.primaryMonitor;
            if (monitor) {
                if (menuX < monitor.x + 8)
                    menuX = monitor.x + 8;
                if (menuX + menuWidth > monitor.x + monitor.width - 8)
                    menuX = monitor.x + monitor.width - menuWidth - 8;
                if (menuY < monitor.y + 8)
                    menuY = badgeY + this._badge.get_height() + 8;
            }

            this._contextMenu.set_position(menuX, menuY);
            return GLib.SOURCE_REMOVE;
        });
        this._trackTimeout(posId);

        // Close menu when clicking elsewhere
        this._menuGrabId = global.stage.connect('button-press-event', (actor, pressEvent) => {
            let [px, py] = pressEvent.get_coords();
            if (!this._contextMenu)
                return Clutter.EVENT_PROPAGATE;

            let [mx, my] = this._contextMenu.get_transformed_position();
            let mw = this._contextMenu.get_width();
            let mh = this._contextMenu.get_height();

            if (px < mx || px > mx + mw || py < my || py > my + mh) {
                this._destroyContextMenu();
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });
    }

    _createMenuItem(text, callback) {
        const BASE = 'padding: 8px 16px; color: white; font-size: 14px;';
        const LIT = `${BASE} background-color: rgba(255,255,255,0.1);`;

        let item = new St.Button({
            child: new St.Label({
                text: text,
                x_align: Clutter.ActorAlign.START,
                x_expand: true,
            }),
            style: BASE,
            reactive: true,
            can_focus: true,
            track_hover: true,
            x_expand: true,
            accessible_name: text,
        });

        // One highlight, two ways to earn it — the keyboard must light the
        // row it is standing on exactly like the pointer does.
        let relight = () => item.set_style(
            item.hover || item.has_key_focus() ? LIT : BASE);
        item.connect('notify::hover', relight);
        item.connect('key-focus-in', relight);
        item.connect('key-focus-out', relight);
        item.connect('clicked', () => callback());

        // The open menu keeps its own focus ring order; this factory only
        // ever builds rows for it, so registering here beats four call sites.
        if (this._contextMenuItems)
            this._contextMenuItems.push(item);

        return item;
    }

    _moveContextMenuFocus(delta) {
        let items = this._contextMenuItems;
        if (!items || items.length === 0)
            return Clutter.EVENT_STOP;
        let current = items.findIndex(i => i.has_key_focus());
        let next = current < 0
            ? (delta > 0 ? 0 : items.length - 1)
            : (current + delta + items.length) % items.length;
        items[next].grab_key_focus();
        return Clutter.EVENT_STOP;
    }

    _destroyContextMenu() {
        if (this._menuGrabId) {
            global.stage.disconnect(this._menuGrabId);
            this._menuGrabId = null;
        }

        let returnTo = this._contextFocusReturn;
        this._contextFocusReturn = null;
        this._contextMenuItems = [];

        if (this._contextMenu) {
            let menu = this._contextMenu;
            this._contextMenu = null;

            // Same rule as the Chronicle: hand focus back only if the menu
            // still holds it.
            let focused = global.stage.get_key_focus();
            let hadFocus = focused && (focused === menu || menu.contains(focused));

            Main.layoutManager.removeChrome(menu);
            menu.destroy();

            if (hadFocus && returnTo && !this._destroyed && this._badge)
                returnTo.grab_key_focus();
        }
    }

    // -- The Chronicle scroll (OSD popup off the 📜 pill) -------------------

    _toggleChronicleScroll(fromKeyboard = false) {
        if (this._chronicleScroll)
            this._destroyChronicleScroll();
        else
            this._showChronicleScroll(fromKeyboard);
    }

    _showChronicleScroll(fromKeyboard = false) {
        if (!this._badge || this._destroyed)
            return;
        this._destroyContextMenu();

        // Only a keyboard-opened scroll takes key focus. A mouse click on
        // chrome must never pull focus out of whatever the user was typing
        // in — but a keyboard user who can't reach the lines has no scroll.
        this._chronicleFocusReturn = fromKeyboard ? this._chroniclePill : null;
        this._chronicleLines = [];

        let scroll = new St.BoxLayout({
            ...boxOrientation(true),
            style_class: 'gnome-speaks-chronicle-scroll',
            reactive: true,
            can_focus: true,
            accessible_role: Atk.Role.PANEL,
            accessible_name: 'The Chronicle — recent speech',
        });
        this._chronicleScroll = scroll;

        // Key handling lives on the container: key events bubble up from the
        // focused line, so one handler covers every line plus the empty case.
        scroll.connect('key-press-event', (actor, event) => {
            let symbol = event.get_key_symbol();
            if (symbol === Clutter.KEY_Escape) {
                this._destroyChronicleScroll();
                return Clutter.EVENT_STOP;
            }
            if (symbol === Clutter.KEY_Down || symbol === Clutter.KEY_Tab)
                return this._moveChronicleFocus(1);
            if (symbol === Clutter.KEY_Up || symbol === Clutter.KEY_ISO_Left_Tab)
                return this._moveChronicleFocus(-1);
            return Clutter.EVENT_PROPAGATE;
        });

        let header = new St.Label({
            text: '📜  The Chronicle',
            style_class: 'gnome-speaks-chronicle-header',
        });
        scroll.add_child(header);

        let addQuietLine = text => {
            let line = new St.Label({
                text,
                style_class: 'gnome-speaks-chronicle-hint',
            });
            scroll.add_child(line);
        };

        if (!this._proxy) {
            addQuietLine('The voice service is not connected');
            this._presentChronicleScroll();
            return;
        }

        this._proxy.GetChronicleRemote(8, (result, error) => {
            if (this._destroyed || this._chronicleScroll !== scroll)
                return;

            let entries = [];
            if (!error && result) {
                try {
                    entries = JSON.parse(result[0]);
                } catch (e) {
                    entries = [];
                }
            }

            if (entries.length === 0) {
                addQuietLine(error ? 'The chronicle is unreachable'
                    : 'Nothing recorded yet — speak, and it will remember');
                this._presentChronicleScroll();
                return;
            }

            entries.reverse();  // newest first
            for (let entry of entries) {
                let isYou = entry.kind === 'you';
                let text = entry.text.replace(/\s+/g, ' ');
                if (text.length > 64)
                    text = `${text.substring(0, 64)}…`;
                // Buttons, not labels: a line you can speak again is an
                // action, and a screen reader should hear it as one.
                let lineLabel = new St.Label({
                    text: `${isYou ? '🗣' : '✨'}  ${text}`,
                    x_align: Clutter.ActorAlign.START,
                    x_expand: true,
                });
                let line = new St.Button({
                    style_class: `gnome-speaks-chronicle-line gnome-speaks-chronicle-line-${isYou ? 'you' : 'it'}`,
                    child: lineLabel,
                    reactive: true,
                    can_focus: true,
                    track_hover: true,
                    x_expand: true,
                    // Dash, not a full stop: the ledger text already ends in
                    // its own punctuation and "you.. Activate" reads badly
                    // out loud — which is the only way this string is read.
                    accessible_name: `${isYou ? 'You said' : 'Spoken'}: ${text} — activate to speak again`,
                });
                line.connect('clicked', actor => {
                    this._proxy.RespeakRemote(entry.id, () => {});
                    // Cast flash: the line lights gold, then the scroll furls.
                    actor.add_style_class_name('gnome-speaks-chronicle-line-cast');
                    let closeId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 240, () => {
                        this._removeTimeout('chronicle-cast-close');
                        if (!this._destroyed)
                            this._destroyChronicleScroll();
                        return GLib.SOURCE_REMOVE;
                    });
                    this._trackTimeout(closeId, 'chronicle-cast-close');
                });
                this._chronicleLines.push(line);
                scroll.add_child(line);
            }

            addQuietLine('a line, spoken again ✦ click or Enter to recast · Esc to furl');
            this._presentChronicleScroll();
        });
    }

    // Arrow keys and Tab walk the ledger. The scroll is not a modal grab, so
    // nothing else moves focus between these lines for us.
    _moveChronicleFocus(delta) {
        let lines = this._chronicleLines;
        if (!lines || lines.length === 0)
            return Clutter.EVENT_STOP;
        let current = lines.findIndex(l => l.has_key_focus());
        let next = current < 0
            ? (delta > 0 ? 0 : lines.length - 1)
            : (current + delta + lines.length) % lines.length;
        lines[next].grab_key_focus();
        return Clutter.EVENT_STOP;
    }

    _presentChronicleScroll() {
        let scroll = this._chronicleScroll;
        if (!scroll || this._destroyed || !this._badge)
            return;

        Main.layoutManager.addTopChrome(scroll);

        // Keyboard-opened: land on the newest line (or the scroll itself when
        // there is nothing to land on) so Escape and the arrows have a home.
        if (this._chronicleFocusReturn) {
            if (this._chronicleLines && this._chronicleLines.length > 0)
                this._chronicleLines[0].grab_key_focus();
            else
                scroll.grab_key_focus();
        }

        let [badgeX, badgeY] = this._badge.get_transformed_position();
        let badgeWidth = this._badge.get_width();

        // Unfurl: rise + fade once allocated (size unknown until then).
        scroll.opacity = 0;
        let posId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            if (!this._chronicleScroll || this._chronicleScroll !== scroll)
                return GLib.SOURCE_REMOVE;

            let w = scroll.get_width();
            let h = scroll.get_height();
            let x = badgeX + (badgeWidth - w) / 2;
            let y = badgeY - h - 12;

            // The badge's OWN monitor: clamping to primaryMonitor sent the
            // scroll to another display whenever the badge lived elsewhere.
            let monitor = this._monitorForBadge();
            if (monitor) {
                x = Math.max(monitor.x + 8,
                    Math.min(x, monitor.x + monitor.width - w - 8));
                if (y < monitor.y + 8)
                    y = badgeY + this._badge.get_height() + 12;
            }

            scroll.set_position(Math.round(x), Math.round(y + 8));
            scroll.ease({
                opacity: 255,
                y: Math.round(y),
                duration: 220,
                mode: Clutter.AnimationMode.EASE_OUT_QUAD,
            });
            return GLib.SOURCE_REMOVE;
        });
        this._trackTimeout(posId);

        this._chronicleGrabId = global.stage.connect('button-press-event', (a, ev) => {
            if (!this._chronicleScroll)
                return Clutter.EVENT_PROPAGATE;
            let [px, py] = ev.get_coords();
            let inside = (actor, w = null, h = null) => {
                if (!actor)
                    return false;
                let [ax, ay] = actor.get_transformed_position();
                let aw = w === null ? actor.get_width() : w;
                let ah = h === null ? actor.get_height() : h;
                return px >= ax && px <= ax + aw && py >= ay && py <= ay + ah;
            };
            // The rune is its own close button: St.Button emits `clicked` on
            // RELEASE, so destroying here would let that release reopen the
            // scroll a frame later — the toggle would look stuck open. Leave
            // it to _toggleChronicleScroll.
            if (inside(this._chroniclePill))
                return Clutter.EVENT_PROPAGATE;
            if (!inside(this._chronicleScroll))
                this._destroyChronicleScroll();
            // Never swallow: a click elsewhere must still reach its target.
            return Clutter.EVENT_PROPAGATE;
        });
    }

    _destroyChronicleScroll() {
        if (this._chronicleGrabId) {
            global.stage.disconnect(this._chronicleGrabId);
            this._chronicleGrabId = null;
        }
        let returnTo = this._chronicleFocusReturn;
        this._chronicleFocusReturn = null;
        this._chronicleLines = [];
        if (this._chronicleScroll) {
            let scroll = this._chronicleScroll;
            this._chronicleScroll = null;

            // Give focus back only if it is still ours to give — the user may
            // have clicked into a window since, and yanking it back there
            // would be a worse bug than the one this fixes.
            let focused = global.stage.get_key_focus();
            let hadFocus = focused &&
                (focused === scroll || scroll.contains(focused));

            scroll.remove_all_transitions();
            Main.layoutManager.removeChrome(scroll);
            scroll.destroy();

            if (hadFocus && returnTo && !this._destroyed && this._badge)
                returnTo.grab_key_focus();
        }
    }

    _trackTimeout(id, name = null) {
        // Deduplicate: cancel any existing timeout with the same name
        // to prevent orphaned timers that fire unexpectedly.
        if (name) this._cancelTimeout(name);
        this._timeouts.push({id, name});
    }

    _cancelTimeout(name) {
        let idx = this._timeouts.findIndex(t => t.name === name);
        if (idx >= 0) {
            // One-shot sources that already fired are gone from the main
            // context; removing them again logs a GLib-CRITICAL (it does
            // NOT throw, so try/catch can't silence it). Probe first.
            if (GLib.MainContext.default().find_source_by_id(this._timeouts[idx].id))
                GLib.Source.remove(this._timeouts[idx].id);
            this._timeouts.splice(idx, 1);
        }
    }

    _removeTimeout(name) {
        let idx = this._timeouts.findIndex(t => t.name === name);
        if (idx >= 0)
            this._timeouts.splice(idx, 1);
    }

    _cancelAllTimeouts() {
        for (let t of this._timeouts) {
            // Same probe as _cancelTimeout: fired one-shots are already
            // gone and a blind remove logs a GLib-CRITICAL at disable.
            if (GLib.MainContext.default().find_source_by_id(t.id))
                GLib.Source.remove(t.id);
        }
        this._timeouts = [];
    }
}
