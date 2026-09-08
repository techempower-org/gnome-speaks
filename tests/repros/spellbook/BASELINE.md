# `spellbook` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `spellbook` | #119, #152 | `025df92` / `a20afea` | **verified** — 8 fail on `025df92`: `cast stop`, `cast halt`, every punctuated trigger (`Cast, stop.`, `Cast - skip`, `Invoke... skip`, …); the denylist, overlay and op-table checks stay green on both sides. The #152 seam checks (`USER_SPELLBOOK_PATH` honours `GS_SPELLBOOK_USER_PATH`; the service reads the seam, not a literal) are red on `a20afea` |
| `spellbook/verify_ha_token` | #129 | `a20afea` | **verified** — 7 of 8 fail (H1 default returns a token / calls `bw`, H3–H6 the config keys are ignored, H7 not synced, H8 personal literals present); H2 (env wins) green on both sides. Token values are never printed: on the maintainer's machine the baseline's personal cache file exists and the default path returns a REAL token |
