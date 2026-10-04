# `http-admission` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `http-admission` | #186 (phase 1) | `9bc6664` | **verified** — the tree has no `HTTP_ALLOWED_ORIGINS` / `_confine_output_file`; reported as the failure without serving a request |
