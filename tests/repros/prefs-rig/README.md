# prefs-rig — a test rig for prefs.js

prefs.js had **no** way to be tested before this. `gnome-extensions prefs
gnome-speaks@jphein` loads the copy installed in
`~/.local/share/gnome-shell/extensions`, **not your worktree**, so it silently
verifies the old file — and the headless-shell harness in CLAUDE.md §Testing
doesn't help either, because gnome-shell never loads prefs.js at all (the prefs
window is a separate process). Written for issue #54 / PR #76.

## What it does

`run.sh` builds a **real, mapped `Adw.PreferencesWindow`** from a prefs.js you
name, on a `gtk4-broadwayd` virtual display. Nothing appears on the live
session and no focus is stolen, so it is safe to run while someone is dictating.
It sandboxes `HOME` and forces `GSETTINGS_BACKEND=memory`, so a run can never
write the real `~/.config/speech-to-cli/config.json` or touch dconf.

```bash
./run.sh <checkout>/prefs.js                                  # one file
./run.sh <checkout>/prefs.js --baseline-rev $(git merge-base origin/main HEAD)
./run.sh <checkout>/prefs.js <some-dir>/prefs.js              # explicit baseline file
```

Relative paths are fine. `--baseline-rev` extracts the baseline into the run
dir, so there is no path of yours for the `>` in a `git show … > <file>` recipe
to land on by accident.

**Baseline against the MERGE BASE, not a moving `origin/main`** — otherwise a
main that advances mid-review manufactures regressions that are not yours.

It reports, for the Audio page: build time, each combo row's options and
selected index **synchronously** and again **after the async probes land**, and
any config write attempted during the fill (must be `none`).

## Always take a baseline

`main` emits **5 pre-existing `Gtk-WARNING`s** (keybinding markup x4, an
unescaped `&` in `Privacy & Debug` x1). A run that shows warnings is not
automatically your fault — pass a second argument to diff against `main` and
judge only the delta.

## The three requirements it exists to enforce

1. **No blocking spawns.** `bench.js` measures the old cost directly:
   `2x wpctl status` = ~20 ms, `6x which` = ~6 ms, vs `find_program_in_path` =
   0.1 ms. Run it on any machine to re-derive the numbers rather than trusting
   these.
2. **An async fill must never write config.** Swapping a `Gtk.StringList` moves
   `selected` and emits `notify::selected`, which `_addComboRow` treats as a
   user choice. The harness monkeypatches `_setConfigValue`/`_deleteConfigKey`
   and prints `WRITES` — anything but `none` means the model swap is silently
   rewriting the setting it is still loading.
3. **A row must not lie while loading.** With `speaker_sink` set in config, the
   synchronous snapshot must show that device *selected*, not "System Default".

## The #116 probe — Local without a server address must warn

`run.sh` also runs `probe_backend_warning.js` against the **branch** file, on a
second rig-owned fixture (`home-local/`: the same pinned keys plus
`speech_backend=local`, `wyoming_host` deliberately absent). Baseline is
skipped on purpose — it predates the feature and would be red by construction,
gating nothing. It asserts on **rendered** label text, never properties:

- the `Primary Provider` subtitle names the dependency and renders;
- exactly one `Adw.Banner` mentioning the Wyoming address exists, is revealed,
  renders, and has a button;
- building the window wrote nothing;
- provider flips hide/show it (those writes are user choices and are asserted
  exactly); typing into `Server Address` alone does **not** clear it, `apply`
  does, clearing it again re-shows it;
- the banner button switches to the page holding the field and focuses it.

Verdict line is `BACKEND_WARN_RESULT ok|FAIL <n>`; a FAIL is a `!! REGRESSION`
(exit 1), a probe that never prints `EXIT_CLEAN` is exit 3. Its stderr is
folded into the branch warning count. Control (measured): against the merge
base before the fix it reports `banner_present FAIL found=0` and exit 1.

Two measured facts it depends on, worth knowing before editing prefs.js:
`Adw.ComboRow` and `Adw.Banner` do **not** parse markup on libadwaita 1.9
(`&amp;` renders verbatim) while `Adw.ActionRow` does — so the subtitle sets
`use_markup = false` explicitly and is assigned after construction, and the
banner title stays free of `<`/`&`. Read `win.get_visible_page()` in a probe,
not the `visible_page` property: the property fast path logs a Gjs-WARNING
about Adw's introspection that the branch itself never emits.

## Failure paths worth re-running

- **Window closed mid-probe** — `sed 's|^win.present();|&\nwin.close();|'` on
  harness.js. Rows must stay on the placeholder, with no crash and no warning.
- **PipeWire down** — put a `wpctl` that `exit 1`s first on `PATH`. Expect empty
  lists, the "No PipeWire devices found" subtitle, and the saved value **not**
  wiped from config.
- **wpctl absent** — `Gio.Subprocess.new` throws; the catch must still call
  `callback([], [])`. (Verified reachable; don't assume it — a dead catch branch
  looks identical to a working one.)

## Config containment — proven, not asserted

`probe_write_containment.js` exercises the REAL writer with the harness's
monkeypatch removed: it calls `_setConfigValue` + `_flushConfigSave` with a
sentinel and checks where it lands.

```
GLib.get_home_dir() = .../wc-<pid>/home     <- honors $HOME
SANDBOX file contains sentinel: true
JP's REAL config: UNCHANGED, sentinel count 0
```

⭐ **Containment comes from `GLib.get_home_dir()` honoring `$HOME`, NOT from the
monkeypatch.** The harness's `_setConfigValue`/`_deleteConfigKey` stubs are a
*reporting* mechanism (they produce the `WRITES` line); they are not what keeps
JP's config safe. That distinction matters because someone will eventually
remove a stub to test a save path -- and this probe says that is safe to do.

⚠️ **This is a GJS/bash suite: 0 `.py` files, by design.** A `find -name '*.py'`
check reports it empty. It runs `gjs` and needs `gtk4-broadwayd`; it will never
be collected by a python test runner.

## Gotchas that cost time

- `BROADWAY_DISPLAY=:7` maps to `broadway8.socket` (off by one). `:8`, `8` and
  `7` all fail with `Failed to open display`.
- The resource import chain needs gnome-shell's private `Shew` typelib:
  `GI_TYPELIB_PATH=/usr/lib/gnome-shell/girepository-1.0` **and**
  `LD_LIBRARY_PATH=/usr/lib/gnome-shell`. The typelib is in
  `/usr/lib/gnome-shell/...`, *not* `/usr/lib/x86_64-linux-gnu/gnome-shell/`.
- `gjs -m -c '<script>'` does not work; the module must be a file.
- In `gjs -m`, `ARGV` is absent — use `import system from 'system'` and
  `system.programArgs`.
