# `extension-gate` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `extension-gate` | 2026-09-10 runaway dictation with the extension disabled | `eb18627` | **verified** — the tree has no `_extension_gate`; the repro reports that as the failure WITHOUT calling `start_listening()` (on that tree it would open the real mic). Wake-watcher case I fails there too: armed + idle + extension absent spawns a recorder and streams to the wake server |
