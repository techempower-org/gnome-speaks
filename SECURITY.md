# Security policy

## Reporting a vulnerability

Please report security problems **privately** to
**security@techempower.org**. Do not open a public issue, pull request or
discussion for an unfixed vulnerability: this repository is public.

A useful report says which version or commit you tested, what an attacker
needs (a local account, a web page open in a browser, network access), and
what they gain. We acknowledge reports within a few days, keep you informed
while we work on a fix, and credit you in the fix and below unless you
prefer to stay anonymous.

Once a fix is merged we track the hardening in a public issue that
describes the problem in general terms only, never step-by-step.

## Supported versions

Fixes land on `main`. There are no maintained release branches; update to
the latest `main` (or the latest tag) to pick up a fix.

## Scope notes

The service exposes an HTTP API on the loopback interface (`127.0.0.1:7710`)
for local agents and tools. That API is open to local callers **by design**;
see the "HTTP API" section of the README for what it admits and why.
Reports about how far that openness reaches (other local users, web pages in
a local browser) are in scope and welcome.

## Acknowledgements

Thank you to the people who reported problems responsibly:

- **[@TheHolm](https://github.com/TheHolm) (Paul Holm)**: reported the
  local HTTP API's exposure to web pages in a local browser (October 2026).
  Fixed in [#187](https://github.com/techempower-org/gnome-speaks/pull/187),
  tracked in [#186](https://github.com/techempower-org/gnome-speaks/issues/186).
