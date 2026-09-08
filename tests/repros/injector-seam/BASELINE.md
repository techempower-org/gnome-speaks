# `injector-seam` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `injector-seam` | #24 | `ea47ff1` | derived (`merge^1`), not re-run |
| `injector-seam/repro_derive_cache` | #136 | `a20afea` | **verified** — 6 fail: 5 acquires issue 5 `list_engines()` + 5 `GetGlobalEngine` (fixed: 1 + 1; a layout switch costs one more `list_engines()`, switching back costs none); the restore/breadcrumb/no-negative-cache/per-bus checks stay green on both sides |
