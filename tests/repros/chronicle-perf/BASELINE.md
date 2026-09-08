# `chronicle-perf` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `chronicle-perf` | #22 / #27 | `6a1ecae` | derived (`merge^1`), not re-run |
| `chronicle-perf/repro_audio_info_stall` | #135 | `025df92` | **verified** — 319 ms stall, probe on MainThread |
