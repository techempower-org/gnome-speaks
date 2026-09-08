# `config-keys` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `config-keys` | #120 #127 | `025df92` | **verified** — B fails: 8 keys against speech-to-cli before its #21 (`language`, `voice_commands` + 6 shell-only `show_*`), 6 after; D fails: the same 6 `show_*` (no Python reader); A, C pass |
