# `quiet-hours` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `quiet-hours` | quiet hours feature (2026-09-12) | `c1f16f0` | **verified** — the tree has no `quiet_hours_active()`; reported as the failure without running the HTTP checks |
