// Standalone prefs harness: loads a prefs.js by path, builds a REAL mapped
// Adw.PreferencesWindow on a broadway display, and reports on the Audio page.
// Usage: gjs -m tmp/harness.js <abs-path-to-prefs.js>
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Gtk from 'gi://Gtk?version=4.0';
import Adw from 'gi://Adw?version=1';
import system from 'system';

Gio.resources_register(Gio.resource_load(
    '/usr/share/gnome-shell/org.gnome.Shell.Extensions.src.gresource'));

const PREFS = system.programArgs[0];
const EXTDIR = GLib.getenv('GS_PREFS_EXTDIR') ||
    (GLib.get_current_dir() + '/fakeext');

Gtk.init();
Adw.init();

const {default: Prefs} = await import(`file://${PREFS}`);

// Pass-through instrumentation: capture rows by title, and flag ANY config
// write, since a programmatic model swap must never write.
const rows = new Map();
const writes = [];
const origCombo = Prefs.prototype._addComboRow;
Prefs.prototype._addComboRow = function (group, title, ...rest) {
    const row = origCombo.call(this, group, title, ...rest);
    rows.set(title, row);
    return row;
};
Prefs.prototype._setConfigValue = function (k, v) { writes.push(`set ${k}=${v}`); };
Prefs.prototype._deleteConfigKey = function (k) { writes.push(`delete ${k}`); };

const dir = Gio.File.new_for_path(EXTDIR);
const metadata = JSON.parse(new TextDecoder().decode(
    GLib.file_get_contents(`${EXTDIR}/metadata.json`)[1]));
metadata.dir = dir;
metadata.path = EXTDIR;

const prefs = new Prefs(metadata);
const win = new Adw.PreferencesWindow();

const t0 = GLib.get_monotonic_time();
prefs.fillPreferencesWindow(win);
const buildMs = (GLib.get_monotonic_time() - t0) / 1000;

const snap = title => {
    const row = rows.get(title);
    if (!row) return '<missing row>';
    const m = row.model;
    const items = [];
    for (let i = 0; i < m.get_n_items(); i++) items.push(m.get_string(i));
    return `selected=${row.get_selected()} [${items.join(' | ')}] sub="${row.subtitle}"`;
};

print(`BUILD_MS ${buildMs.toFixed(1)}`);
print(`SYNC Speaker    ${snap('Speaker')}`);
print(`SYNC Microphone ${snap('Microphone')}`);
print(`SYNC Player     ${snap('Player')}`);
print(`SYNC Recorder   ${snap('Recorder')}`);

win.present();

const loop = new GLib.MainLoop(null, false);
GLib.timeout_add(GLib.PRIORITY_DEFAULT, 2500, () => {
    print(`ASYNC Speaker    ${snap('Speaker')}`);
    print(`ASYNC Microphone ${snap('Microphone')}`);
    function* walk(w) {
        if (!w) return;
        yield w;
        let c = w.get_first_child();
        while (c) { yield* walk(c); c = c.get_next_sibling(); }
    }
    // A markup failure leaves the PROPERTY intact and the LABEL empty, so a
    // property snapshot cannot see it. Anything whose text is set but renders
    // blank is the signature -- generic, no per-row knowledge required.
    const blanks = [];
    for (const w of walk(win)) {
        const isRow = w instanceof Adw.ActionRow;
        const isGrp = w instanceof Adw.PreferencesGroup;
        if (!isRow && !isGrp) continue;
        const shown = new Set([...walk(w)]
            .filter(x => x instanceof Gtk.Label).map(x => x.get_text()));
        for (const [prop, val] of [['title', w.title],
            ['subtitle', isRow ? w.subtitle : null]]) {
            if (!val) continue;
            // markup is stripped when rendered, so compare on the escaped form too
            const plain = val.replace(/&amp;/g, '&').replace(/&lt;/g, '<')
                .replace(/&gt;/g, '>');
            if (!shown.has(val) && !shown.has(plain))
                blanks.push(`${w.constructor.$gtype.name}.${prop}="${val}"`);
        }
    }
    print(`BLANK_RENDERED ${blanks.length === 0 ? 'none' : blanks.length + ' -> ' + blanks.join(' ; ')}`);
    // Positive assertion, not just absence: for every markup-risky string
    // (contains '<' or '&'), show property vs what get_text() actually returns.
    for (const w of walk(win)) {
        const isRow = w instanceof Adw.ActionRow;
        const isGrp = w instanceof Adw.PreferencesGroup;
        if (!isRow && !isGrp) continue;
        const shown = [...walk(w)].filter(x => x instanceof Gtk.Label).map(x => x.get_text());
        for (const [prop, val] of [['title', w.title], ['subtitle', isRow ? w.subtitle : null]]) {
            if (!val || !/[<&]/.test(val)) continue;
            const plain = val.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
            const hit = shown.find(t => t === val || t === plain);
            print(`RENDERED ${prop} property="${val}" -> get_text()=` +
                (hit === undefined ? '"" <<BLANK>>' : `"${hit}" ${hit === plain ? 'MATCHES' : '?'}`));
        }
    }
    print(`WRITES ${writes.length === 0 ? 'none' : writes.join(', ')}`);
    win.close();
    loop.quit();
    return GLib.SOURCE_REMOVE;
});
loop.run();
print('EXIT_CLEAN');
