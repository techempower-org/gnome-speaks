# `call-mute` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `call-mute` | video-call mute (2026-09-18) | `fb3e2a5` | **verified** — the tree has no `_call_muted()`; reported as the failure. Wake-watcher case J fails there too (armed + idle + on a call spawns and streams) |
