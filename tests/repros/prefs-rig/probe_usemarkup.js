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
// Does use_markup:false reach the SUBTITLE, or only the title?
const off = new Adw.ActionRow({title: 'Toggle Listening',
    subtitle: '<Super><Alt>space', use_markup: false});
// And does it survive a LATER subtitle assignment (the dialog's save path)?
const later = new Adw.ActionRow({title: 'Later', use_markup: false});
g.add(off); g.add(later); page.add(g); win.add(page); win.present();
later.subtitle = '<Super><Alt>c';

GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
    print(`use_markup:false at construction -> ${labels(off)}`);
    print(`use_markup:false, subtitle set LATER -> ${labels(later)}`);
    print(`(any Gtk-WARNING above means it did NOT cover the subtitle)`);
    win.close(); loop.quit(); return GLib.SOURCE_REMOVE;
});
const loop = new GLib.MainLoop(null, false); loop.run();
