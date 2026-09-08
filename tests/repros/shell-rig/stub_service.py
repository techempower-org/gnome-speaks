#!/usr/bin/env python3
"""Own org.gnome.Speaks on the CURRENT session bus and answer GetState.

Rig-only. Under dbus-run-session that bus is private, so this never collides
with the real service. `--state X` is what GetState answers AND what a
StateChanged signal announces one second after the name is acquired, so
the rig can park the badge in a non-idle state before the stub is killed
(a service dying mid-utterance is the interesting vanish).
"""
import sys
import gi
gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib

STATE = sys.argv[sys.argv.index('--state') + 1] if '--state' in sys.argv else 'idle'
XML = """<node><interface name="org.gnome.Speaks">
  <method name="GetState"><arg direction="out" type="s"/></method>
  <signal name="StateChanged"><arg type="s"/></signal>
</interface></node>"""
info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
conn = None


def on_call(c, sender, path, iface, method, params, inv):
    if method == 'GetState':
        inv.return_value(GLib.Variant('(s)', (STATE,)))
    else:
        inv.return_dbus_error('org.gnome.Speaks.Stub', 'stub does not implement ' + method)


def announce():
    conn.emit_signal(None, '/org/gnome/Speaks', 'org.gnome.Speaks', 'StateChanged',
                     GLib.Variant('(s)', (STATE,)))
    print('stub: announced', STATE, flush=True)
    return GLib.SOURCE_REMOVE


def on_acquired(c, name):
    global conn
    conn = c
    c.register_object('/org/gnome/Speaks', info, on_call, None, None)
    print('stub: owns', name, flush=True)
    GLib.timeout_add(1000, announce)


Gio.bus_own_name(Gio.BusType.SESSION, 'org.gnome.Speaks', Gio.BusNameOwnerFlags.NONE,
                 None, on_acquired, lambda c, n: print('stub: lost', n, flush=True))
GLib.MainLoop().run()
