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
| `injector-seam/repro_no_global_engine_noise` | #177 | `a715022` | **verified** — 7 fail: N1/N2 (`restore_prior_engine()` with no breadcrumb and no engine set: one libibus `No global engine` line on stderr per call, no INFO), N3 (one line per first `acquire()` on a bus), N4 (one line even in the real crash state — breadcrumb present, engine unset — which is restored on both sides). stderr is captured at the **fd** level because the line is C-side; N0 is the positive control (the fake's libibus-shaped `get_global_engine()` must be seen, else SETUP FAILURE). N5 (an engine IS set: the property answers a serialized `EngineDesc`) is a guard, green on both sides |
