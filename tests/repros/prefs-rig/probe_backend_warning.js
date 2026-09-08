// Probe for #116: Speech Backend → Primary Provider = Local must WARN when no
// Wyoming server address is set, and the warning must track both inputs.
//
// Runs against a fixture with speech_backend=local and NO wyoming_host (run.sh
// writes it to a second sandbox HOME). Everything is asserted on RENDERED text
// -- a markup failure keeps the property and blanks the label, so a property
// snapshot would pass a warning nobody can read.
//
// Usage (run.sh does this): gjs -m probe_backend_warning.js <ABSOLUTE prefs.js>
// Prints one `BACKEND_WARN <check> PASS|FAIL …` line per check, then
// `BACKEND_WARN_RESULT ok|FAIL <n>` and `EXIT_CLEAN`.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Gtk from 'gi://Gtk?version=4.0';
import Adw from 'gi://Adw?version=1';
import system from 'system';

Gio.resources_register(Gio.resource_load(
    '/usr/share/gnome-shell/org.gnome.Shell.Extensions.src.gresource'));

const PREFS = system.programArgs[0];
if (!PREFS || !GLib.path_is_absolute(PREFS)) {
    printerr(`probe_backend_warning.js: need an ABSOLUTE path to prefs.js, got "${PREFS ?? ''}".`);
    system.exit(2);
}
const EXTDIR = GLib.getenv('GS_PREFS_EXTDIR') ||
    (GLib.get_current_dir() + '/fakeext');

Gtk.init();
Adw.init();

const {default: Prefs} = await import(`file://${PREFS}`);

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
prefs.fillPreferencesWindow(win);
win.present();

function* walk(w) {
    if (!w) return;
    yield w;
    let c = w.get_first_child();
    while (c) { yield* walk(c); c = c.get_next_sibling(); }
}
const rendered = w => [...walk(w)].filter(x => x instanceof Gtk.Label).map(x => x.get_text());
const unescape = s => s.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');

let fails = 0;
const check = (name, ok, detail) => {
    if (!ok) fails++;
    print(`BACKEND_WARN ${name} ${ok ? 'PASS' : 'FAIL'} ${detail}`);
};

// Fixture sanity: this probe means nothing against the wrong config.
check('fixture', prefs._config.speech_backend === 'local' && !prefs._config.wyoming_host,
    `speech_backend=${prefs._config.speech_backend} wyoming_host=${JSON.stringify(prefs._config.wyoming_host ?? null)}`);

const loop = new GLib.MainLoop(null, false);
GLib.timeout_add(GLib.PRIORITY_DEFAULT, 600, () => {
    const combo = rows.get('Primary Provider');
    check('combo_row', !!combo, combo ? `selected=${combo.get_selected()}` : '<missing row>');

    // 1. The combo's subtitle names the dependency, and RENDERS.
    const sub = combo?.subtitle ?? '';
    const subShown = combo ? rendered(combo) : [];
    const subHit = subShown.find(t => t === sub || t === unescape(sub));
    check('subtitle_names_dependency', /Offline Server/.test(sub) && /Wyoming server address/i.test(sub),
        `property="${sub}"`);
    check('subtitle_rendered', subHit !== undefined,
        subHit !== undefined ? `get_text()="${subHit}"` : `get_text()="" <<BLANK>> (labels: ${subShown.map(t => JSON.stringify(t)).join(',')})`);

    // 2. Exactly one revealed banner mentions the Wyoming address, and RENDERS.
    const banners = [...walk(win)].filter(w => w instanceof Adw.Banner && /Wyoming/.test(w.title));
    check('banner_present', banners.length === 1, `found=${banners.length}`);
    const banner = banners[0];
    if (banner) {
        check('banner_revealed_local_nohost', banner.revealed === true, `revealed=${banner.revealed}`);
        const shown = rendered(banner);
        const hit = shown.find(t => t === banner.title || t === unescape(banner.title));
        check('banner_rendered', hit !== undefined,
            hit !== undefined ? `get_text()="${hit}"` : `get_text()="" <<BLANK>> (labels: ${shown.map(t => JSON.stringify(t)).join(',')})`);
        check('banner_has_button', !!banner.button_label, `button_label="${banner.button_label}"`);
    }
    // 3. Building the window wrote NOTHING.
    check('no_writes_on_build', writes.length === 0, writes.length ? writes.join(', ') : 'none');

    if (combo && banner) {
        // 4. Provider flips hide/show it. These ARE user choices, so the
        //    writes they produce are expected and asserted exactly.
        const before = writes.length;
        combo.set_selected(0);            // azure
        check('hides_on_azure', banner.revealed === false, `revealed=${banner.revealed}`);
        combo.set_selected(1);            // local
        check('shows_on_local', banner.revealed === true, `revealed=${banner.revealed}`);
        check('toggle_writes_exact',
            writes.slice(before).join(', ') === 'set speech_backend=azure, set speech_backend=local',
            writes.slice(before).join(', ') || 'none');

        // 5. The address row: typing alone must NOT clear it; apply must.
        const host = [...walk(win)].find(w => w instanceof Adw.EntryRow && w.title === 'Server Address');
        check('host_row', !!host, host ? 'found Server Address' : '<missing row>');
        if (host) {
            host.set_text('wyoming.example');
            check('typing_alone_keeps_warning', banner.revealed === true, `revealed=${banner.revealed}`);
            host.emit('apply');
            check('hides_on_host_apply', banner.revealed === false, `revealed=${banner.revealed}`);
            host.set_text('   ');
            host.emit('apply');
            check('shows_on_host_cleared', banner.revealed === true, `revealed=${banner.revealed}`);

            // 6. The button jumps to the page that holds the field.
            const pageOf = w => { for (let p = w; p; p = p.get_parent()) if (p instanceof Adw.PreferencesPage) return p; return null; };
            const hostPage = pageOf(host);
            banner.emit('button-clicked');
            // get_visible_page(), not the property: GJS's property fast path
            // logs a Gjs-WARNING about Adw's visible-page introspection, and
            // a probe must not manufacture stderr the branch did not.
            const vis = win.get_visible_page();
            check('button_switches_page', vis === hostPage,
                `visible_page="${vis?.title}" host_page="${hostPage?.title}"`);
            const focus = win.get_focus();
            let focusInHost = false;
            for (let p = focus; p; p = p.get_parent()) if (p === host) { focusInHost = true; break; }
            check('button_focuses_host_row', focusInHost,
                `focus=${focus ? focus.constructor.$gtype.name : 'null'}`);
        }
    }

    print(`BACKEND_WARN_RESULT ${fails === 0 ? 'ok' : 'FAIL ' + fails}`);
    win.close();
    loop.quit();
    return GLib.SOURCE_REMOVE;
});
loop.run();
print('EXIT_CLEAN');
