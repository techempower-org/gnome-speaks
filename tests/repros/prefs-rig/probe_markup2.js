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
const bad  = new Adw.PreferencesGroup({title: 'Privacy & Debug'});
const good = new Adw.PreferencesGroup({title: 'Privacy &amp; Debug'});
// Does a combo's StringList label get markup-parsed? Device names can hold '&'.
const combo = new Adw.ComboRow({title: 'Speaker',
    model: Gtk.StringList.new(['Scarlett Solo & Co', 'Plain Device'])});
bad.add(combo); page.add(bad); page.add(good); win.add(page); win.present();

GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
    print(`GROUP TITLE unescaped '&' renders: ${labels(bad).split(' , ')[0]}`);
    print(`GROUP TITLE escaped '&amp;' renders: ${labels(good)}`);
    print(`COMBO row labels (device w/ '&'): ${labels(combo)}`);
    win.close(); loop.quit(); return GLib.SOURCE_REMOVE;
});
const loop = new GLib.MainLoop(null, false); loop.run();
