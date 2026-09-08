# `cancel-tokens` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `cancel-tokens` | #21 / #33 | `66edc79` | **verified** — 4 of 5 fail |
| `cancel-tokens` `repro_e` | #132 | `025df92` | **verified** — E1, E3 fail; E2 stays green on both sides |
| `cancel-tokens` `repro_f` | #132 / PR #146 review | `fd5c8cd` (PR head before revision), `0e014cc` | **verified** — F1 43/126 and 51/300 dictations hit, F2 1/1; 0/101 and clean with the fix. F1 lowers `sys.setswitchinterval` (GS_RACE_SWITCH, default 1e-5) — at CPython's 5 ms default it scored 0/300 on the unfixed tree |
| `cancel-tokens` `repro_g` | #167 | `99e0214` | **verified** — G1 fails (3 checks: state idle with the STT thread alive, agent item spoken over the mic, item 'done'); G2, G3 green on both sides. This is repro_f's residual `state 'idle'` red made deterministic — no interrupt involved |
