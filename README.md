# Callwitness
<!-- mcp-name: io.github.AditiChaudharyy14/callwitness -->

**Record every tool call an AI agent makes. Block nothing.**

[![tests](https://github.com/AditiChaudharyy14/callwitness/actions/workflows/tests.yml/badge.svg)](https://github.com/AditiChaudharyy14/callwitness/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/callwitness)](https://pypi.org/project/callwitness/)
**[callwitness.tech](https://callwitness.tech)** · [Install from PyPI](https://pypi.org/project/callwitness/)

A transparent MCP proxy. It sits between an agent and its tools, forwards every
byte unchanged, and writes down what happened.

No dependencies. Python 3.8+. MIT.

---

## Thirty seconds

```bash
pip install callwitness
callwitness demo
```

If `pip` is not a command on your machine, `python -m pip install callwitness`
does the same thing.

If the install finishes with a warning that the script went somewhere **not on
PATH**, the `callwitness` command will not exist. Run it as a module instead --
same tool, same output:

```bash
python -m callwitness demo
python -m callwitness last
```


No agent, no API key, nothing to configure. It starts a real MCP server through
the recorder, calls the tools that read like reads, refuses the ones that don't,
shows what came back, and tells you where your responses sit against 140 calls
measured across 65 public servers.

```
  npx -y @modelcontextprotocol/server-everything declared 8 tools.
  6 read like reads; 2 refused by the safety rule and never called.

    echo                              41 B
    get-resource-reference           369 B
    get-structured-content           187 B

  6 calls recorded. Nothing was blocked, nothing was altered.

  Your calls against the public baseline (65 servers, 140 calls)

    server-everything/echo            41 B   p12  vs same tool    3x smaller
    server-everything/get-resource   369 B   p61  vs same tool    about typical
```

Point it at your own server instead:

```bash
callwitness demo -- npx -y @your/server
```

---

## The thing it shows you

Same tool. Same permission. Two very different actions:

```
      51B  send_email   ops@acme.com
    20085B send_email   exfil.example.net, drop@unknown.example
```

An allowlist cannot tell those apart — the agent is permitted to send email in
both cases. The difference is *how much* is leaving and *where it is going*, and
those are the two signals Callwitness records on every call.

## Why it blocks nothing

Because it should be installable in production on a Tuesday afternoon.

Callwitness cannot corrupt what an agent sends or receives: it relays every message
whether or not it can parse it, and every write to storage is wrapped so a
recorder bug can't reach the stream. That property is tested, not asserted —
see `tests/test_passthrough.py`, which asserts the proxied output is
byte-identical to running the server directly.

**It cannot delay, either.** Observation runs on its own thread behind a bounded
queue, so the relay only ever does a non-blocking hand-off. A deliberately
half-second-slow observer moves the gap between two forwarded messages by 0.05ms
— it used to move it by 4.2 seconds. The queue drops rather than growing without
limit under load, and counts what it dropped: unrecorded data nobody can see is
worse than data that was never collected.

It matters because the security industry is currently writing rules against
agent failures nobody has measured. Enforcement without data is guessing with
extra steps. Collect first.

## Use

Wrap any stdio MCP server:

```bash
callwitness run --echo -- npx -y @modelcontextprotocol/server-filesystem /data
```

Or let it wrap the servers you already have. It finds your client's config,
shows you exactly what would change, and writes nothing until you say so:

```bash
$ callwitness install

/Users/you/Library/Application Support/Claude/claude_desktop_config.json
  filesystem
    - npx -y @modelcontextprotocol/server-filesystem /data
    + callwitness run --label filesystem -- npx -y @modelcontextprotocol/server-filesystem /data
  git
    - uvx mcp-server-git --repository /repo
    + callwitness run --label git -- uvx mcp-server-git --repository /repo
  remote-api  SKIPPED: remote server -- needs `callwitness proxy --upstream
              https://mcp.acme.com/mcp --port <port>` and a port you choose

2 servers would be wrapped. Nothing has been changed.
```

`--apply` writes it, after a timestamped backup. `callwitness uninstall --apply`
puts everything back. Running install twice does nothing the second time.

Dry-run is the default because this edits a file you did not write and a broken
MCP config means a broken agent — the one outcome this whole tool promises not
to cause. Anything it does not recognise is skipped and named rather than
guessed at.

Knows about Claude Desktop, Cursor, Windsurf, Claude Code, and project-local
`.mcp.json` / `.vscode/mcp.json`. A single config can hold more than one server
map — Claude Code keeps a global one and a separate one per project under
`projects.<path>.mcpServers` — and every one of them is walked. When the answer
is "nothing to wrap", install prints every location it checked, because *no
config exists*, *no servers in it* and *already wrapped* are three different
situations and only one of them means you are finished.

If yours lives somewhere else:
`callwitness install --config /path/to/mcp.json`.

### Remote servers

Production agents mostly talk to remote MCP servers over Streamable HTTP. Put
Callwitness in front of one and point the client at the local address instead:

```bash
callwitness proxy --upstream https://mcp.example.com/mcp --port 8100 --echo
```

```json
{
  "mcpServers": {
    "example": { "url": "http://127.0.0.1:8100/mcp" }
  }
}
```

POST, the SSE response stream, the server-initiated `GET` stream and session
teardown are all relayed verbatim, headers included, so the `Mcp-Session-Id`
handshake works without Callwitness understanding it. Both transports share one
recorder (`CallTracker`), so a row looks the same whichever produced it.

The agent behaves exactly as before. Then look at what it did:

```bash
callwitness last             # what happened in the last run: failures, the biggest responses
callwitness cost --since 7d  # what your tools returned, estimated in tokens
callwitness tail --errors    # only the calls that failed
callwitness stats            # per-tool volume, errors, latency, destinations
callwitness verify           # check nothing has been altered since it was written
callwitness export out.jsonl # everything, for analysis
```

### What happened in that run

`stats` answers which tools exist, which is nobody's question. The two people
actually have are *what broke* and *what was enormous*, and both are about one
run rather than every run ever recorded.

```bash
$ callwitness last

  fetch   17 Sep 05:56 -> 06:02   open, idle 1h
  2 calls, 0 failed, 71.0 KB returned, 3.4 s in tools

  nothing failed

  biggest responses
    fetch                          68.6 KB      2.1 s  17 Sep 06:01
    fetch                           2.4 KB      1.2 s  17 Sep 06:02

  where it went
    callwitness.tech                 2

  Every call:  callwitness tail --session 6d4e47f7
```

Failures come first, then the biggest responses, then anything slow that was
not already listed. If nothing failed it says so in one line and moves on.

`callwitness last --runs` lists recent runs, and is careful about what it
claims. A session with no recorded end reads *still running* only while calls
are still arriving; after an hour of silence it reads *open, idle 3d*. That is
the honest form — the process may have been killed, or it may be sitting there
alive and unused, and the record cannot tell you which. Saying *no end
recorded* invited the first conclusion; saying *still running* asserted the
second. Knowing for certain would mean storing a pid and testing liveness, and
on Windows the obvious test terminates the process rather than checking it.

The arrow points at the last call rather than at now, for the same reason:
nothing is known to have happened after it.

Then drill in. `--session` takes the id `last` prints, and matches on a prefix:

```bash
callwitness tail --session 6d4e47f7
callwitness tail --errors
```

`--errors` is the one worth running on a bad day:

```
  13 Sep 08:19  ERR  tool_get_definition     in=23   out=0 B    16.2 s
  13 Sep 08:21  ERR  feed                    in=38   out=0 B    37.3 s
  13 Sep 08:25  ERR  get_gcores_new          in=2    out=0 B    21.2 s
```

Three calls that took between sixteen and thirty-seven seconds to return
nothing. An agent waits that out and moves on without saying anything, and the
same rows sit invisibly in the middle of a 180-row `stats` table. Those three
are from the census sweep of 86 public servers rather than from my own agent --
`--errors` spans the whole database, which is the point of it.

### What it is costing you

The recorder measures bytes because bytes are a fact. Nobody budgets in bytes.

```bash
$ callwitness cost --since 7d

  tool                          calls   returned      ~tokens   worst call
  deepwiki_fetch                    3     1.4 MB     ~366,246     684.9 KB
  list_directory                    4     1.0 MB     ~251,216     981.3 KB
  fetch                             5    71.8 KB      ~18,374      68.6 KB
  ... and 130 more tools

  total                           222     3.8 MB   ~1,001,351
```

The ratio is an estimate, it is printed on every run, and it is a flag
(`--bytes-per-token`), because a constant nobody can see is a constant nobody
can correct. Four bytes per token is a middle figure for JSON — worse with
dense punctuation or non-ASCII, better for prose.

Failed calls are excluded: returning nothing costs no context, however long it
took. Those belong in `tail --errors`, where they are.

And it counts what tools returned and nothing else — not the schemas your
client sends at startup, not your prompts, not the model's replies. Someone
will hold this next to their bill, so it says so itself.

### What it cannot see

Callwitness is a proxy for MCP traffic. That is the whole of what it records.

An agent doing work through its own built-in tools is invisible here. Claude
Code, for instance, reads files and runs commands natively and only reaches for
an MCP server when it has no built-in equivalent — so wrapping its servers
records the edges of what it does, not the middle. Agents that are MCP-driven
by construction, which is most custom agents and most Cursor or Windsurf setups
with real servers wired up, are recorded completely.

Direct API calls made inside agent code are the same blind spot and would need
an SDK wrapper, which is deliberately not in v1.

### Where your numbers sit

A recording tells you what happened. It cannot tell you whether what happened is
normal — a number is only unusual relative to something, and on day one your own
history is empty, which is exactly when you are deciding whether this tool is
worth keeping.

```bash
callwitness baseline --compare
```

```
  npm:mcp-deepwiki/deepwiki_fetch      684.9 KB  p100  vs same tool  11x the median for this tool
  npm:mcp-trends-hub/get_bbc_news       13.5 KB  p100  vs same tool  high end
  npm:@primeng/mcp/version               1.0 KB  p100  vs same tool  high end
```

The reference is the published census — the same `callwitness.baseline.v1`
document this command emits, measured across public servers and served at
[callwitness.tech/baseline/v1.json](https://callwitness.tech/baseline/v1.json).
Each row says what it was compared against, because a comparison against the
whole distribution is much weaker than one against the same tool, and a weak
comparison should look weak.

**The comparison is a download.** Nothing about your traffic is sent. Sharing is
a separate command you have to type, described below.

### Then let it write the rules

The next tier was going to be YAML you write by hand. But a person typing
`max_payload: 8KB` for `send_email` is guessing at a number they have no way to
know — which is the thing this project says the industry is doing wrong.
Enforcement without data is guessing with extra steps, and a rule language is
not data. So the rules come out of the observation tier instead:

```
$ callwitness suggest --since 14d

  send_email     max_payload            3.5KB    # p99 observed 2.3KB over n=400; 1.5x headroom
  send_email     destinations_emails    3 allowed # 3 distinct emails covering 100% of traffic over n=400
  send_email     rate_limit_per_hour    21       # 10.0/hour average over 39.9 hours; 2x headroom
? fetch_url      destinations_hosts     --       # 99 distinct hosts across 150 calls -- too varied for an
                                                 #   allowlist; this reads as a general-purpose fetcher
? delete_record  insufficient_data      --       # only 6 calls observed; 30 needed before a threshold
                                                 #   means anything
```

`--format yaml` emits the same thing as a policy draft, every rule commented
with the evidence it rests on.

Note what it refuses to do. A tool below 30 calls gets no threshold, because a
p99 over n=6 is an anecdote. A tool whose destinations are too varied is flagged
for a human rather than handed an allowlist that would fire constantly. And a
destination that was never seen is not a destination that is forbidden — it may
simply not have happened yet, and the output says so rather than letting you
forget it. Every line is a hypothesis with its evidence attached, not a finding.

**Baseline poisoning.** If the bad thing already happened while Callwitness was
watching, it is in the distribution, and a plain percentile quietly raises the
ceiling to permit it. The demo above showed exactly that: a 29KB exfiltration
produced a 43KB proposed ceiling — one that would have allowed the very call
this tool exists to catch.

So ceilings come from the bulk of a distribution, not all of it. Calls far above
the median set no limit; they are named, with timestamps and destinations, and
handed to a person:

```
  send_email  max_payload   4.3KB  # p99 of the bulk is 2.9KB; 1.5x headroom.
                                   #   EXCLUDES 1 call above 11.4KB
! send_email  tail_review   1      # 1 call more than 8x the 1.4KB median. A rare
                                   #   enormous call is the most interesting thing
                                   #   here, so it sets no limit until you have
                                   #   looked at it: 28.8KB at 2026-09-09T18:09
                                   #   -> exfil.example.net
```

The reference is the median, because it is the one statistic a single enormous
call cannot move — which is the point when that call may be the attack. If more
than 10% of traffic sits above the threshold it is not a tail, it is the shape,
and nothing is excluded; misdescribing the distribution is a different failure,
and just as wrong.

This is not a solution to baseline poisoning. Nothing that learns from unlabelled
traffic has one. It is a refusal to hide it.

### Generating enough traffic for `suggest`

`callwitness demo` records a handful of calls, which is enough to watch the tool
work but not enough for `suggest` to say anything responsible — it refuses to
propose a threshold below 30 calls. For that, a local generator with no agent,
no API key, no network and no Node:

```bash
python examples/demo.py                     # throwaway run, nothing kept
python examples/demo.py --keep --repeat 40  # record into your own store
callwitness suggest                         # then let it propose rules
```

The plain run uses a temporary directory so trying the tool doesn't pollute
anyone's data — but the obvious next thing to type is `callwitness stats`, and
"No data yet" is a bad first hour. `--keep` records into `~/.callwitness`, and
`--repeat` sends enough varied traffic that `suggest` has a distribution to
work from rather than an anecdote.

## Privacy

| Flag | Effect |
|---|---|
| *(default)* | Credentials in argument values are redacted before storage |
| `--no-redact` | Stores argument values verbatim, credentials included |
| `--no-args` | Stores argument *shape* only (`{"to": "<str:20>"}`), never values |
| `--max-arg-bytes N` | Caps stored bytes; the true size is still recorded |
| `--home DIR` | Where data lives (default `~/.callwitness`) |

**Redaction is on by default.** Tool arguments routinely carry API keys, bearer
tokens and connection strings, and without this every install would be a
plaintext credential store that didn't exist before Callwitness was installed. Known
key formats, credentials inside URLs, sensitively-named parameters and
high-entropy tokens are replaced with `<redacted:reason>` on the write path —
never on read, because by then the plaintext is already on disk. The true
pre-redaction byte count is still recorded, so the volume signal survives.

Destinations survive redaction on purpose: `postgres://admin:hunter2@db.internal`
stores as `postgres://admin:<redacted:url_password>@db.internal`. The host is the
signal; the password is not.

`--no-args` still records destinations — hosts, emails, IPs — because
destinations are the signal. That's deliberate, it's tested, and you should say
it out loud to anyone you ask to run this.

Everything stays on the machine that ran it. Nothing is transmitted anywhere.

### Evidence, not just a log

An append-only file is trivially editable by anyone with filesystem access —
including a compromised agent running as the same user. A record that can be
silently rewritten is a convenience, not evidence.

So every call commits to the one before it. Editing, deleting, reordering or
inserting a record breaks the chain from that point, and `callwitness verify` says
where:

```
$ callwitness verify
BROKEN  filesystem  642 records, breaks at seq 118
                    content does not match its hash: this record was edited
                    after it was written
```

Exit code 1 on a break, so it works in a cron job without anyone parsing text.

**It is tamper-evident, not tamper-proof, and the tool says so out loud.**
Someone who can write to the file can also recompute every hash after a change
and produce a chain that verifies — nothing local can stop that, because the
verifier and the attacker read the same file. What defeats it is an anchor the
operator does not control, so `verify` prints the head hash and tells you to
store it somewhere the machine cannot reach. That is a deployment decision, and
inventing one for you would be worse than naming the gap.

Records written before chaining existed are reported as predating it, not as
tampering. A verifier that cries wolf on an upgraded install is worse than no
verifier.

## What gets stored

`calls` — one row per tool call: tool, arguments, `args_bytes`, whether they
were truncated, `signals`, `duration_ms`, `is_error`, `result_bytes`, and a
result preview.

`signals` splits two things a naive scan conflates:

```json
{
  "destinations":   {"emails": ["archive@unknown-host.example"],
                     "hosts":  ["exfil.example.net"]},
  "content_counts": {"emails": 400}
}
```

**Destinations** are entities found in routing fields — `to`, `url`, `webhook`,
`attach_url` and so on. **Content counts** are how many entities appear in the
payload. Scanning the whole blob for email addresses would report four hundred
customer emails from inside a message body as "destinations" and bury the one
address the message is actually addressed to. These are different signals and
they compose: *29KB addressed to an unknown host, containing 400 email
addresses* is a shape worth stopping. *29KB containing 400 addresses, sent to
the CRM you always use* is a Tuesday.

`events` — `initialize` and `tools/list`, so you know which tools were exposed
and how many bytes their schemas cost.

`sessions` — one row per wrapped process, with the exit code.

SQLite at `~/.callwitness/callwitness.db`, plus an append-only `calls.jsonl`.

## The public baseline

`callwitness contribute` sends the *shape* of your tool traffic — which public
packages you wrap, how many calls, how many bytes came back, how long it took —
to a public baseline. Nothing else.

```
callwitness contribute --dry-run   # print the exact bytes, send nothing
callwitness contribute --enable    # off until you type this
callwitness contribute --send      # shows the payload, asks, then sends
```

Never sent: arguments, paths, filenames, hostnames, results, or any part of
them — including hashed, which is not anonymisation when the input space is
small enough to enumerate. A server that is not a published package is reported
as `unlisted` and its tool names become `tool_1`, `tool_2`. A published package
identifies software; a path identifies an organisation.

Nothing is sent while the proxy is running. There is no thread, no timer and no
`atexit` hook — the only route to the network is a command you type. Your
install id is a random UUID kept locally, and it is the only way to ask for your
data to be removed.

The collector is 190 lines of JavaScript in [`worker/index.js`](worker/index.js).
It validates against the same key list the client enforces, rejects unknown
fields rather than stripping them, and never reads the caller's IP. Both halves
of that promise are in this repository, so you can check them instead of
trusting them.

Full specification: [docs/CONTRIBUTE.md](docs/CONTRIBUTE.md)

## What it found

The census that `--compare` reads is itself the finding: **declared size does not
predict delivered size.** Every published estimate of MCP context cost counts the
schemas a server declares in `tools/list`; nothing counted what servers actually
return, because that means running them.

```
  mcp-deepwiki       declares 774 B,  returned 62 KB
  company-registry   declares 145 KB, returned 4 KB
  mcp-sympy          declares 63 KB across 171 tools, returned 198 B
```

Median response 580 B, p95 36 KB, largest 496 KB — a 6,437x spread across 140
calls on 65 servers. The sharpest version is one server measured twice: deepwiki
declares 774 bytes both times, and returned 701 KB for a large repository and
62 KB for a small one. Same server, same declared size, 11x apart, because the
argument differed.

Method, every server that failed and why, and the rule that decided which tools
were safe to call: [callwitness.tech/research](https://callwitness.tech/research/).
Results: [callwitness.tech/baseline](https://callwitness.tech/baseline/).

## Design rule

The recorder must never corrupt the protocol stream, and must never delay it.
Every message is forwarded first, then handed to a background queue; parsing
happens on another thread, inside a `try`. If recording throws, traffic still
flows. If recording is slow, traffic still moves.

If you contribute, keep it that way. `test_a_broken_recorder_never_raises` and
`tests/test_hardening.py` are there to make sure you do.

## The experiment

`experiments/` runs the measurement this tool exists to make possible: 15 tasks
x 4 injection channels, an agent with real tools and real side effects, all
traffic recorded through Callwitness itself.

```bash
python experiments/run.py --driver scripted --out runs/pilot --fresh --repeats 4
python experiments/analyze_runs.py runs/pilot
```

That validates the pipeline with no API key and no network. Swap
`--driver llm` with a Groq free-tier key for the real thing. Protocol and
design are in [experiments/README.md](experiments/README.md).

## Where this is going

1. **Now** — observe. Record every call, block nothing.
2. **Next** — deterministic policy: the rules `callwitness suggest` proposes,
   evaluated inline, sub-millisecond, fail-open by default. The generator ships
   first on purpose; an engine that enforces numbers nobody could justify is the
   problem, not the product.
3. **Then** — context: an LLM judge, but only on calls the deterministic tier
   flags. Payload volume × destination reputation first.

Scope: MCP tool calls over stdio and Streamable HTTP. The deprecated
two-endpoint HTTP+SSE transport is not covered. Direct API calls made inside
agent code need an SDK wrapper, and that is deliberately not in v1.

## Where it came from

Out of an experiment on chain-of-thought faithfulness
([cot-hint-verbalization](https://github.com/AditiChaudharyy14/cot-hint-verbalization)),
which turned up a measurement problem: "hint verbalisation rate" reads 100% on
the reasoning trace and 12% on the user-facing answer, for the same responses.
Same data, same model, an order of magnitude apart depending only on where you
look.

A field whose headline metric moves by 10x depending on the instrument does not
need another opinion about agent risk. It needs somebody to start writing down
what actually happens.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
