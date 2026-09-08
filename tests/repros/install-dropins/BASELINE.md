# `install-dropins` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `install-dropins` | #115 / #103 | `a20afea` | **not re-run, by design** — that `install.sh` ignores unknown flags and would perform a full install (and restart the live service) when handed `--check-dropins`; the suite refuses any installer without the flag (SETUP FAILURE 2, verified against `a20afea`), so discrimination is by construction |
