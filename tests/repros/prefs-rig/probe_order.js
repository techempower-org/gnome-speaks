import GLib from 'gi://GLib';
import Gtk from 'gi://Gtk?version=4.0';
import Adw from 'gi://Adw?version=1';
Gtk.init(); Adw.init();
function* walk(w) { yield w; let c = w.get_first_child();
    while (c) { yield* walk(c); c = c.get_next_sibling(); } }
const labels = w => [...walk(w)].filter(x => x instanceof Gtk.Label)
    .map(l => `"${l.get_text()}"`).join(' , ');
const win = new Adw.PreferencesWindow();
const page = new Adw.PreferencesPage();
const g = new Adw.PreferencesGroup({title: 'probe'});

print('--- A: use_markup FIRST in the literal ---');
const a = new Adw.ActionRow({use_markup: false, title: 'A', subtitle: '<Super><Alt>space'});
print('--- B: use_markup LAST in the literal ---');
const b = new Adw.ActionRow({title: 'B', subtitle: '<Super><Alt>c', use_markup: false});
print('--- C: construct without subtitle, assign after ---');
const c = new Adw.ActionRow({title: 'C', use_markup: false});
c.subtitle = '<Super><Alt>r';
print('--- end (warnings above are attributable to the block they follow) ---');
g.add(a); g.add(b); g.add(c); page.add(g); win.add(page); win.present();
GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
    print(`A -> ${labels(a)}`);
    print(`B -> ${labels(b)}`);
    print(`C -> ${labels(c)}`);
    win.close(); loop.quit(); return GLib.SOURCE_REMOVE;
});
const loop = new GLib.MainLoop(null, false); loop.run();
