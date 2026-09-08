# `deprecations` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `deprecations` | #114 | `a20afea` | **verified** — 3 deprecation lines at service start (`GLib.unix_signal_add` ×2, `Gio.DBusConnection.register_object` ×1 — PyGObject warns once per deprecated GI function per process, so two register sites make one line); `GetState` and the Spiel `Name` property answer on both sides. Runs the real `main()` on a private `dbus-run-session` bus the suite re-execs itself under; `register_object` also leaked ~1.5 kB per D-Bus method call (measured; flat through `register_object_with_closures2`) |
