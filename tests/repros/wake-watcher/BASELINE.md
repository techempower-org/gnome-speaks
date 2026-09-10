# `wake-watcher` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `wake-watcher` | #41, #48 / #121, #137 | `11c8f60`, `a20afea` | **verified** — pre-#41 `11c8f60`: B fails (25-spawn storm, zero sleeps). Pre-#137 `a20afea`: G fails (second recorder after 10 s of fake time, not 0.5) and H fails (a WARNING and a 10 s sleep logged *during shutdown* — the very lines #137 misread as start-time failures); B, C, F fail there too because they assert the bounded ramp before the unchanged 10 s / 60 s steady cadence. A, D, E green on every side. The fake `time.sleep` is scoped to the watcher thread by identity — the constructor also starts `tts-queue-dispatcher`, whose 0.2 s hold-polls were being recorded and stopped (A doubles as that guard: ~240 ms window, dispatcher must survive) |
| `wake-watcher` case I | extension master switch (2026-09-10) | `eb18627` | **verified** — I fails: armed + idle + no `org.gnome.Speaks.Desktop` owner spawns a recorder and calls `detect_stream`. A–H unchanged |
