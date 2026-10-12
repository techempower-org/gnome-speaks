# Changelog

Notable changes to GNOME Speaks. Older history is in the git log and the
annotated tags (`git tag -n5`).

## Unreleased

### Security

- **Local HTTP API admission** ([#186](https://github.com/techempower-org/gnome-speaks/issues/186)
  phase 1, [#187](https://github.com/techempower-org/gnome-speaks/pull/187)).
  The loopback API on `:7710` now answers browser requests only from origins
  listed in `GS_HTTP_ALLOWED_ORIGINS` (empty by default) and only for loopback
  `Host` names. It no longer sends wildcard CORS headers, and browser POSTs must
  be JSON. `output_file` is confined to `$XDG_CACHE_HOME/gnome-speaks/out/`.
  `POST /cast` now honours the master switch, quiet hours and call mute the same
  way `POST /speak` does, and `/api/version` no longer reports the hostname.
  Agents and `curl` callers (no `Origin` header) work unchanged.
  Reported by [@TheHolm](https://github.com/TheHolm) (Paul Holm). Thank you.
