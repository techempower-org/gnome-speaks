#!/usr/bin/env bash
# lint (#186 phase 2): the per-install token must never ride in a command line.
# /proc/<pid>/cmdline is readable by every local user, which is exactly the
# threat the token exists to close. Fails on any tracked file (docs included:
# a recipe is copied verbatim) where a curl call passes a token header with -H
# / --header (header names in any case: HTTP ignores it), or reads the token
# file inside a command substitution (cat/head/tail/<).
# Callers use `curl -K ~/.config/gnome-speaks/curl.conf` or `curl -K -` (stdin).
# exit 0 = clean; 1 = a hit (printed); 2 = setup failure.
set -u
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
command -v git >/dev/null || { echo "!! SETUP FAILURE: no git"; exit 2; }
SELF="tests/repros/http-token/lint_token_argv.sh"
PAT='(-H|--header)[[:space:]=]*["'"'"']?(X-Speaks-Token|Authorization:[[:space:]]*Bearer)|\$\((cat|head|tail|<)[^)]*gnome-speaks/token'
# Positive control: the pattern must see the forms it forbids, or a clean
# result means nothing.
for probe in 'curl -H "X-Speaks-Token: $T" http://127.0.0.1:7710/x' \
             "curl --header 'Authorization: Bearer abc' u" \
             'tok=$(cat ~/.config/gnome-speaks/token)' \
             'curl -H "x-speaks-token: $T" u' "curl -H 'authorization: bearer abc' u" \
             'tok=$(head -c 64 ~/.config/gnome-speaks/token)'; do
  printf '%s\n' "$probe" | grep -qiE "$PAT" || { echo "!! SETUP FAILURE: pattern misses: $probe"; exit 2; }
done
# Negative control: the sanctioned forms pass.
for ok in 'curl -K ~/.config/gnome-speaks/curl.conf -X POST u' 'printf x | curl -q -K - u'; do
  printf '%s\n' "$ok" | grep -qiE "$PAT" && { echo "!! SETUP FAILURE: pattern flags the sanctioned form: $ok"; exit 2; }
done
# A git checkout: scan the tracked files. An extracted tree (`git archive`, how
# run_all.sh is pointed at a SHA) has no index of its own -- git would answer
# for an enclosing repo with zero files here, a vacuous pass -- so scan the
# tree itself instead.
ntracked=$(git -C "$REPO" ls-files -- . 2>/dev/null | wc -l)
if [ "$ntracked" -gt 0 ]; then
  hits=$(git -C "$REPO" grep -niE "$PAT" -- . ":(exclude)$SELF" 2>/dev/null); scope="$ntracked tracked files"
else
  hits=$(cd "$REPO" && grep -rniE --exclude-dir=.git --exclude-dir=tmp --exclude-dir=scratch \
           --exclude-dir=__pycache__ "$PAT" . 2>/dev/null | grep -v "^./$SELF:")
  nfiles=$(cd "$REPO" && find . -path ./tmp -prune -o -path ./.git -prune -o -type f -print | wc -l)
  [ "$nfiles" -gt 0 ] || { echo "!! SETUP FAILURE: nothing to scan under $REPO"; exit 2; }
  scope="$nfiles files (no git index)"
fi
if [ -n "$hits" ]; then
  echo "FAIL: token in a command line:"
  printf '%s\n' "$hits"
  exit 1
fi
echo "PASS: no token header or token-file read in any command line ($scope)"
exit 0
