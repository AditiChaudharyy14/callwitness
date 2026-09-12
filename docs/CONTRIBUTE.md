# `callwitness contribute` — specification

Status: draft for review. Nothing here is implemented yet.
Target: ships with v0.2, in the same release as the census paper. Never before.

---

## What it is

An opt-in way for someone running callwitness to add the *shape* of their tool
traffic to a public baseline — how much came back, how often, how long it took —
without sending arguments, paths, filenames, hostnames or content.

## Why it exists

Today every callwitness install produces recordings that stay on one machine.
Ten thousand installs would leave the dataset exactly where it is now: one
person's census, run by hand. A measurement company whose dataset does not grow
with adoption is a blog with a CLI attached.

The aggregate is published back to everyone, contributor or not. That is the
exchange, and it has to be visible or nobody opts in.

---

## Non-negotiables

**1. Off by default.** No prompt, no nag. Someone who installs callwitness and
never reads this document sends nothing, forever.

**2. Nothing leaves during a proxy run.** Callwitness promises it cannot delay
the stream. A network call on the hot path breaks that promise even when it
succeeds. Contribution is a separate command the operator runs on their own
schedule. No background thread, no atexit hook, no daemon.

**3. Auditable, not promised.** `--dry-run` prints the exact bytes that would be
sent. Not a summary — the payload. A privacy claim that can only be verified by
reading source is worth less than one you can check in a terminal in five
seconds.

**4. Shape only.** The interesting number — how much came back — is not
sensitive. The sensitive part is not needed. If a field could identify a person,
a company, a repository or a file, it does not ship, even if it would make the
baseline better.

**5. Reversible, with a handle.** The contributor holds an install id. It is the
only way to ask for their data to be removed, so it is printed on enable and
stored locally in plain text.

---

## Commands

```
callwitness contribute --status      # off, or on since <date>, with the install id
callwitness contribute --enable      # generates an install id, prints it, turns it on
callwitness contribute --disable     # stops. Local id kept so removal is still possible.
callwitness contribute --forget      # disable, delete the local id, print how to request removal
callwitness contribute --dry-run     # print the exact payload, send nothing
callwitness contribute --send        # print the payload, ask y/n, then send
```

`--send` shows the payload and waits for confirmation every time until the
operator passes `--yes`. Confirmation is not theatre: it is how someone notices
the day the payload changes.

---

## The payload

One JSON document per send. Versioned from day one; a schema that changes
without a version is how a dataset becomes unmergeable.

```json
{
  "schema": "callwitness.contribution.v1",
  "install": "b0f1c2d3-4e5f-4a6b-8c9d-0e1f2a3b4c5d",
  "version": "0.2.0",
  "platform": "windows",
  "python": "3.11",
  "window": { "from": "2026-09-06T00:00:00Z", "to": "2026-09-13T00:00:00Z" },
  "servers": [
    {
      "package": "npm:@modelcontextprotocol/server-filesystem",
      "declared_bytes": 13018,
      "tool_count": 14,
      "tools": [
        {
          "tool": "list_directory",
          "calls": 41,
          "errors": 2,
          "returned_bytes": { "min": 180, "p50": 4210, "p95": 129400, "max": 334987 },
          "argument_bytes": { "p50": 38, "max": 220 },
          "latency_ms": { "p50": 12, "p95": 340 }
        }
      ]
    },
    {
      "package": "unlisted",
      "declared_bytes": 8800,
      "tool_count": 6,
      "tools": [
        {
          "tool": "tool_1",
          "calls": 12,
          "errors": 0,
          "returned_bytes": { "min": 96, "p50": 310, "p95": 2200, "max": 2400 },
          "argument_bytes": { "p50": 44, "max": 90 },
          "latency_ms": { "p50": 8, "p95": 26 }
        }
      ]
    }
  ]
}
```

### Field rules

**`install`** — a random UUID4 generated once on `--enable`. Not derived from
the hostname, MAC address, username or anything else. Two installs on one
machine are two ids, and that is correct: the unit is an installation, not a
person.

**`package`** — the public package specifier, and only when it is public:

| wrapped command | `package` |
|---|---|
| `npx -y @scope/name` | `npm:@scope/name` |
| `npx -y name` | `npm:name` |
| `uvx name` | `pypi:name` |
| `python -m thing`, `./server.js`, `/opt/internal/bin/x` | `unlisted` |

A company's internal server is named by its path or its binary, and both are
identifying. Those become `unlisted`, keeping the size distribution — which is
the point — and discarding the identity.

**`tool`** — the real tool name only when `package` is not `unlisted`. Tool
names on a public package are already public. On an unlisted server they are the
company's vocabulary, so they become `tool_1`, `tool_2`, … stable within one
payload and meaningless outside it.

**`returned_bytes`, `argument_bytes`, `latency_ms`** — summary statistics, never
per-call values. A sequence of exact byte counts is a fingerprint of a specific
session; a p50 and a p95 are not. `argument_bytes` is the *size* of the
arguments. The arguments themselves never leave, in any form, including hashed.

**`window`** — the period covered, so a weekly contributor and a monthly one are
not double-counted.

### What is never sent

Arguments. Paths. Filenames. Hostnames or URLs. Tool results, in whole, in part,
truncated or hashed. Environment variables. The operator's username, hostname,
IP or timezone. Timestamps of individual calls. Anything from the hash chain.

Destination hosts are **out of scope for v1** even in categorised form. They are
the most valuable field and the easiest to get wrong, and shipping the safe
version first is how the safe version stays the version.

---

## `--dry-run` output

```
$ callwitness contribute --dry-run

contribute is OFF. This is what would be sent if you enabled it.

  window     2026-09-06 .. 2026-09-13
  servers    3  (2 public packages, 1 unlisted)
  tools      21
  calls      412
  payload    4,118 bytes

{ ...the exact JSON, pretty-printed, in full... }

Not sent. Nothing has left this machine.
To send it:  callwitness contribute --enable  then  callwitness contribute --send
```

The full JSON prints. Not a sample. If it is too long to read, that is
information about the payload and the operator should have it.

---

## The opt-in copy

Shown by `--enable`, before it does anything:

```
callwitness contribute

  Sends the SHAPE of your tool traffic to a public baseline: which public
  packages you wrap, how many calls, how many bytes came back, how long it
  took. Nothing else.

  Never sent:  arguments, paths, filenames, hostnames, results, or any part
               of them -- including hashed.

  Nothing is sent automatically. Nothing is sent while the proxy is running.
  You run `callwitness contribute --send` yourself, and it shows you the
  payload and asks before every send.

  See exactly what would leave:  callwitness contribute --dry-run
  The published baseline:        https://callwitness.tech/baseline

  Your install id is 5a1c... -- the only way to ask for your data to be
  removed. It is stored at ~/.callwitness/contribute.json.

Enable? [y/N]
```

---

## Where it goes

No server exists yet, and one should not be built.

**Recommended: a Cloudflare Worker.** Free tier, an account already exists for
the site's analytics, no machine to patch, no bill to fail to pay. The Worker
validates the schema and rejects anything with an unexpected field — a payload
containing a key this spec does not list is a bug that must fail loudly, not be
stored — then appends to KV or R2.

**Rejected: a VPS.** Something to maintain, secure and pay for.

**Rejected: a third-party analytics SaaS.** Cannot promise what leaves if the
destination is someone else's black box.

**Considered: a GitHub PR per contribution.** No infrastructure at all and
public by construction, which is appealing. Rejected because it requires a
GitHub account and makes the contribution attributable, which is the opposite of
the design.

The endpoint ships in the code as a constant so anyone can read where the bytes
go, and is overridable with `CALLWITNESS_CONTRIBUTE_URL` so an organisation can
point it at their own collector.

---

## Retention

Contributions are kept indefinitely, because the whole point is the series.
No contribution is ever published on its own — only aggregates across at least
**five distinct installs**, so a single contributor's profile cannot be read out
of the baseline. An install id can be removed on request, taking effect in the
next published baseline.

That last sentence is a promise. It has to be implementable before it is
printed, which means the store is keyed by install id from the first write.

---

## Gate

Week 8 of the plan: **did at least 5% of installs opt in?**

If yes, the loop turns and every later decision is judged on whether it grows
the baseline.

If no, that is the most useful negative result available. Either the exchange is
not visible enough — publish the baseline more often, show contributors their
effect — or people will not share this data at all, in which case the
compounding business does not exist and callwitness is a research-led
consultancy with good tooling. That is a real business. It is a different one,
and it is better to know at week 8 than at month eight.

---

## Open questions

1. **Automatic sending, ever?** This spec says never. A weekly `--send` the
   operator forgets to run means a baseline that stops growing. The alternative
   is a scheduled sender — a background network call in a tool whose whole
   promise is that it does nothing you did not ask for. Current answer: manual,
   and if adoption is the problem, fix it with a reminder in `stats` output
   rather than a daemon.

2. **Should `unlisted` servers contribute at all?** Including them makes the
   baseline representative of real deployments rather than just public packages.
   Excluding them removes a class of risk. Current answer: include, identity
   stripped, because private servers are exactly where the surprising numbers
   will be.

3. **Five installs is a guess.** It is the threshold below which an aggregate
   could identify a contributor. Check what a privacy engineer would actually
   use before printing it as a promise.