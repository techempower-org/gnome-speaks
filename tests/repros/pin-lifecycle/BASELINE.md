# `pin-lifecycle` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `pin-lifecycle` | #46 ×#57 | `15dd402` | compound X: X1 FAIL, X2 PASS, X3 FAIL |
