# `type-tag-split` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `type-tag-split` | #193 | `60aa19d` | **not executable** — `_route_type_tokens` was introduced by #193, so the repro cannot load a router there (traceback, exit 1). Red control instead: the current router with `pending = carry + pending` dropped fails the two split-tag cases (verified 2026-11-13) |
