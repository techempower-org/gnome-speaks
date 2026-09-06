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
const grp = new Adw.PreferencesGroup({title: 'Privacy & Debug'});
const rawRow = new Adw.ActionRow({title: 'Toggle Listening', subtitle: '<Super><Alt>space'});
const escRow = new Adw.ActionRow({title: 'Escaped',
    subtitle: GLib.markup_escape_text('<Super><Alt>space', -1)});
const [okParse, key, mods] = Gtk.accelerator_parse('<Super><Alt>space');
const human = okParse ? Gtk.accelerator_get_label(key, mods) : '(unparseable)';
const humanRow = new Adw.ActionRow({title: 'Human', subtitle: human});
grp.add(rawRow); grp.add(escRow); grp.add(humanRow);
page.add(grp); win.add(page); win.present();

GLib.timeout_add(GLib.PRIORITY_DEFAULT, 400, () => {
    print(`subtitle PROPERTY (raw row) : "${rawRow.subtitle}"`);
    print(`RENDERED labels, raw row    : ${labels(rawRow)}`);
    print(`RENDERED labels, escaped    : ${labels(escRow)}`);
    print(`RENDERED labels, human      : ${labels(humanRow)}`);
    print(`accelerator_get_label()     : "${human}"`);
    print(`group has use-markup prop?  : ${'use_markup' in grp}`);
    print(`row has subtitle-use-markup?: ${GLib.getenv('X') === null ? ('use_markup' in rawRow) : ''}`);
    win.close(); loop.quit(); return GLib.SOURCE_REMOVE;
});
const loop = new GLib.MainLoop(null, false); loop.run();
