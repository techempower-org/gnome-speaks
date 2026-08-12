# Preferences redesign — task-first, search-honest, audit-fixed

Research inputs (both live-verified, on the workstation):
`~/.claude/projects/-home-jp/scratch/prefs-redesign/hig-research.md` (IA +
rules; key finding: Adw 1.5 search never matches group descriptions — our 29
paragraphs were unsearchable) and `control-audit.md` (101-row inventory; 5
dead controls, entry help text never rendered, split-brain subtitle toggle,
lying defaults, duplicated groups).

## IA: 10 pages → 6, task-first

`Dictation · AI Chat · Voice & Sound · Wake & Spells · Accounts · Advanced`

- Azure page dissolves: creds → Accounts, voices → Voice & Sound, STT
  tuning → Dictation. Cloud AI page → Accounts (creds) + AI Chat
  (generation). Modes/Listening → Dictation. Feedback → Voice & Sound.
  Extension → Dictation › Starting. Spellcraft → Wake & Spells.
- Barge-in unified in AI Chat (was split across two pages).
- All silence timeouts co-located in Dictation › Flow.

## Rules applied (from HIG research)

R1 explain on the row: group descriptions ≤3 total, subtitles everywhere.
R2 money/destruction visible: Adw.Banner on Accounts (metering), cost
subtitles on HD voice / max-tokens / Spiel-Azure. R3 one control per row.
R5 no subsystem words in titles ("Speak While Listening", not "Half Duplex").
R6 defaults stated in subtitles. (Deviation: no per-group Reset buttons —
wiring cost outweighs value for a personal tool; revisit on demand.)

## Audit fixes in this rebuild

1. Dead controls dropped: Speed/Pitch/Volume/VAD-aggressiveness (params the
   service never passes — follow-up issue to wire speed properly).
2. `_addEntryRow`/`_addPasswordRow` help text: Adw.EntryRow has no subtitle —
   help renders as tooltip + critical caveats (wake-model privacy) get
   explicit info rows. Password rows no longer nest a PreferencesGroup
   inside a group (invalid Adw).
3. Live Subtitles: ONE switch writing both GSettings `live-subtitles` and
   config `live_subtitles` (was split-brain with opposite owners).
4. Deprecated `Adw.MessageDialog` → `Adw.AlertDialog`.
5. Duplicates removed: one Shortcuts group, one Restart row.
6. Truthful text: ydotool not wtype; echo-cancel default aligned to the
   service's `false`.
7. Gaps added: Spiel provider group (3 keys), `save_audio_dir`
   (privacy-labeled), `conversation_silence_timeout`.

Also: `llm_model` default unified to the dotted canonical (state.py had
hyphens, prefs dots — llm_stream's MODEL_MAP speaks dots).

Known-deferred (issue to file): prefs and service both rewrite config.json
whole-file on save (last writer wins) — needs read-merge-write.
