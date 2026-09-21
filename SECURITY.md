# Security policy

## Reporting a vulnerability

Please report security issues privately, not in a public issue.

- GitHub: **Security** tab → **Report a vulnerability**
- Email: callwitness.dev@gmail.com

Include what you ran, what you expected, and what happened. You will get a
reply within 72 hours.

## Supported versions

Only the latest release on PyPI gets fixes. Upgrade with:

    pip install -U callwitness

## What callwitness does and does not claim

- It forwards every byte and blocks nothing. It is a recorder, not a firewall.
- Records are hash-chained, which makes them **tamper-evident, not tamper-proof**:
  editing a record breaks `verify`, but someone with write access to the store
  can rewrite the whole chain. External anchoring is not built yet.
- Redaction is on by default and runs before anything is written to disk.
- Nothing leaves your machine unless you run `callwitness contribute`.

If any of these turns out to be false in practice, that is a security bug.
Please report it.
