# `tts-prefetch` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `tts-prefetch` | #134 (×#146 for p4) | `a20afea` | **verified** — p1 fails (wall 3.01 s serial vs 2.21 s pipelined, S=0.4/P=0.6); **p2, p3, p4 report `2` (SETUP FAILURE) there, not `0`** — the prepared-ahead window they test does not exist on a service that never prefetches, and a suite that passed on it would be measuring nothing. p4 is a guard: `skip_current()` (the #146 interrupt path) is a no-op while a reply holds the queue, and the queue path has no prefetch — it goes red if either premise changes |
