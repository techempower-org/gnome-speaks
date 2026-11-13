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
| `chronicle-perf/verify_retention` | #186 (phase 1, item 7) | `535250c` | **verified** — crashes on the missing `_CHRONICLE_RETENTION_DAYS` (printed as `CRASH: AttributeError`), rc=1 |
| `chronicle-perf/verify_retention` R11–R12 | #191 | `7b8026e` | **verified** — 3 FAIL: R11 the temp in the checked directory survives and the swapped-in decoy is unlinked (path-based sweep); R12 a 0755 directory is swept. R1–R10 and the R12 control pass |
