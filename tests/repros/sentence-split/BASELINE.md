# `sentence-split` — baseline

The pinned SHA(s) this suite discriminates against and what fails there.
`tests/repros/gen_readme.sh` collects the table rows below (every line that
starts with `| ` except the header and separator) into the generated block in
`tests/repros/README.md` — edit THIS file, then run the generator. Pin SHAs,
never branch names (see README, "Baselines are pinned SHAs").

| suite | issue / PR | baseline | expected there |
|---|---|---|---|
| `sentence-split` | #154 | `5dde4b4` | **verified** — s1 fails (2 synthesis calls: `Beta two follows.Gamma three ends.` spoken and subtitled as one); s2 fails 7 of 13 rows — the issue's `. ` token, two boundaries in one token, `\n`, double space, `!`/`?`, an ellipsis, `e.g.` — every shape where the buffer ends in `[.!?]` + whitespace with a second boundary before it. Guards green on both sides: tokenizer-shaped tokens, `3.5`, fullwidth punctuation (not a boundary, unchanged), no terminal punctuation |
