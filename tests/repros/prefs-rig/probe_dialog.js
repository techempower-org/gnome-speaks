import GLib from 'gi://GLib';
import Gtk from 'gi://Gtk?version=4.0';
import Adw from 'gi://Adw?version=1';
Gtk.init(); Adw.init();
function* walk(w) { yield w; let c = w.get_first_child();
    while (c) { yield* walk(c); c = c.get_next_sibling(); } }
const labels = w => [...walk(w)].filter(x => x instanceof Gtk.Label)
    .map(l => `"${l.get_text()}"`).join(' , ');
const win = new Adw.PreferencesWindow(); win.present();
const BODY = 'Enter a keyboard shortcut (e.g. <Super><Alt>space):';
print('--- A: body as written on main ---');
const a = new Adw.AlertDialog({heading: 'Set shortcut', body: BODY});
print('--- B: body_use_markup false, body assigned after ---');
const b = new Adw.AlertDialog({heading: 'Set shortcut'});
b.body_use_markup = false; b.body = BODY;
print('--- C: escaped body ---');
const c = new Adw.AlertDialog({heading: 'Set shortcut',
    body: GLib.markup_escape_text(BODY, -1)});
print('--- end ---');
print(`has body_use_markup? ${'body_use_markup' in a}`);
a.present(win); 
GLib.timeout_add(GLib.PRIORITY_DEFAULT, 400, () => {
    print(`A rendered: ${labels(a)}`);
    a.close(); b.present(win);
    GLib.timeout_add(GLib.PRIORITY_DEFAULT, 300, () => {
        print(`B rendered: ${labels(b)}`);
        b.close(); c.present(win);
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 300, () => {
            print(`C rendered: ${labels(c)}`);
            c.close(); win.close(); loop.quit(); return GLib.SOURCE_REMOVE;
        });
        return GLib.SOURCE_REMOVE;
    });
    return GLib.SOURCE_REMOVE;
});
const loop = new GLib.MainLoop(null, false); loop.run();
