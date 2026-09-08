// Rig-only probe. Lives INSIDE the headless shell so it can read what no
// outside instrument can: the computed St.ThemeNode of the gnome-speaks
// badge, its accessible name, the label's visibility, the panel menu rows,
// and the notifications Main.notify produced. It drives the badge through
// the same seams a pointer or keyboard would (_onBadgeClicked, hover,
// key focus) and never touches the real desktop -- it only ever runs in
// the sandbox run.sh builds.
import Gio from 'gi://Gio';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const TARGET = 'gnome-speaks@jphein';

const IFACE = `<node>
  <interface name="org.gnome.Speaks.RigProbe">
    <method name="Dump"><arg direction="out" type="s" name="json"/></method>
    <method name="SetHover"><arg direction="in" type="b" name="on"/></method>
    <method name="SetFocus"><arg direction="in" type="b" name="on"/></method>
    <method name="Tap"/>
    <method name="TapAndDump"><arg direction="out" type="s" name="json"/></method>
    <method name="Call"><arg direction="in" type="s" name="method"/></method>
  </interface>
</node>`;

function color(c) {
    try {
        return c ? c.to_string() : null;
    } catch (e) {
        return `?${e.message}`;
    }
}

export default class RigProbe extends Extension {
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, '/org/gnome/Speaks/RigProbe');
        this._ownerId = Gio.bus_own_name_on_connection(Gio.DBus.session,
            'org.gnome.Speaks.RigProbe', Gio.BusNameOwnerFlags.NONE, null, null);
    }

    disable() {
        if (this._ownerId) {
            Gio.bus_unown_name(this._ownerId);
            this._ownerId = 0;
        }
        if (this._dbus) {
            this._dbus.unexport();
            this._dbus = null;
        }
    }

    _target() {
        let ext = Main.extensionManager.lookup(TARGET);
        return ext ? ext.stateObj : null;
    }

    Dump() {
        let out = {target_loaded: false};
        let t = this._target();
        if (!t) return JSON.stringify(out);
        out.target_loaded = true;
        out.state = t._state;
        out.service_start_pending = t._serviceStartPending;
        out.notify_is_function = typeof Main.notify === 'function';
        let badge = t._badge;
        if (badge) {
            let node = null;
            try { node = badge.get_theme_node(); } catch (e) { out.badge_node_error = e.message; }
            out.badge = {
                classes: badge.get_style_class_name(),
                accessible_name: badge.accessible_name,
                hover: badge.hover,
                has_key_focus: badge.has_key_focus(),
                width: badge.get_width(),
                bg: node ? color(node.get_background_color()) : null,
                border: node ? color(node.get_border_color(St.Side.TOP)) : null,
                padding_left: node ? node.get_padding(St.Side.LEFT) : null,
            };
        }
        if (t._icon) {
            let node = null;
            try { node = t._icon.get_theme_node(); } catch (e) { /* off-stage */ }
            out.icon = {
                name: t._icon.icon_name,
                color: node ? color(node.get_foreground_color()) : null,
                size: node ? node.get_length('icon-size') : null,
            };
        }
        if (t._label) {
            let node = null;
            try { node = t._label.get_theme_node(); } catch (e) { /* off-stage */ }
            out.label = {
                visible: t._label.visible,
                text: t._label.text,
                color: node ? color(node.get_foreground_color()) : null,
            };
        }
        if (t._pills)
            out.pills_visible = t._pills.map(p => p.visible);
        if (t._panelIcon) {
            let node = null;
            try { node = t._panelIcon.get_theme_node(); } catch (e) { /* off-stage */ }
            out.panel_icon = {
                name: t._panelIcon.icon_name,
                classes: t._panelIcon.get_style_class_name(),
                color: node ? color(node.get_foreground_color()) : null,
            };
        }
        let row = item => item ? {text: item.label.text, sensitive: item.sensitive} : null;
        out.menu = {
            service: row(t._menuServiceItem),
            listen: row(t._menuListenItem),
            stop: row(t._menuStopItem),
        };
        try {
            out.notifications = Main.messageTray.getSources()
                .flatMap(s => s.notifications || [])
                .map(n => ({title: n.title, body: n.body}));
        } catch (e) {
            out.notifications_error = e.message;
        }
        return JSON.stringify(out);
    }

    SetHover(on) {
        let t = this._target();
        if (t && t._badge) t._badge.hover = on;
    }

    SetFocus(on) {
        let t = this._target();
        if (!t || !t._badge) return;
        if (on) t._badge.grab_key_focus();
        else global.stage.set_key_focus(null);
    }

    Tap() {
        let t = this._target();
        if (!t) return;
        t._releaseKeyFocusFromPointer();
        t._onBadgeClicked();
    }

    // The tap and the dump in ONE main-loop turn: the systemctl child
    // cannot have exited yet, so this is the only honest look at the
    // "Starting…" state on a machine where the sandboxed spawn fails fast.
    TapAndDump() {
        this.Tap();
        return this.Dump();
    }

    Call(method) {
        let t = this._target();
        if (t) t._callMethod(method);
    }
}
