// Does an UNPATCHED config write stay in the sandbox? The main harness stubs
// _setConfigValue/_deleteConfigKey, so the real _saveConfig path is never
// exercised there -- meaning containment was asserted, not proven. If HOME
// sandboxing did not cover it, a rig run would edit JP's live config while he
// is dictating. This exercises the real writer deliberately.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Gtk from 'gi://Gtk?version=4.0';
import Adw from 'gi://Adw?version=1';
import system from 'system';
Gio.resources_register(Gio.resource_load(
    '/usr/share/gnome-shell/org.gnome.Shell.Extensions.src.gresource'));
Gtk.init(); Adw.init();
const PREFS = system.programArgs[0];
const EXTDIR = GLib.getenv('GS_PREFS_EXTDIR');
const {default: Prefs} = await import(`file://${PREFS}`);
const dir = Gio.File.new_for_path(EXTDIR);
const metadata = JSON.parse(new TextDecoder().decode(
    GLib.file_get_contents(`${EXTDIR}/metadata.json`)[1]));
metadata.dir = dir; metadata.path = EXTDIR;
const prefs = new Prefs(metadata);
const win = new Adw.PreferencesWindow();
prefs.fillPreferencesWindow(win);          // NO monkeypatch this time
print(`GLib.get_home_dir() = ${GLib.get_home_dir()}`);
prefs._setConfigValue('speaker_sink', 'SENTINEL-DO-NOT-SHIP');
prefs._flushConfigSave();                  // force the real writer to run
const sandbox = `${GLib.get_home_dir()}/.config/speech-to-cli/config.json`;
const [ok, buf] = GLib.file_get_contents(sandbox);
print(`SANDBOX file contains sentinel: ${ok && new TextDecoder().decode(buf).includes('SENTINEL-DO-NOT-SHIP')}`);
win.close();
