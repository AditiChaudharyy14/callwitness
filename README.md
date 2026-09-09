# Bollard

**Record every tool call an AI agent makes. Block nothing.**

A transparent MCP proxy. It sits between an agent and its tools, forwards every
byte unchanged, and writes down what happened.

No dependencies. Python 3.8+. MIT.

---

## The thing it shows you

Same tool. Same permission. Two very different actions:

```
      51B  send_email   ops@acme.com
    20085B send_email   exfil.example.net, drop@unknown.example
```

An allowlist cannot tell those apart — the agent is permitted to send email in
both cases. The difference is *how much* is leaving and *where it is going*, and
those are the two signals Bollard records on every call.

## Why it blocks nothing

Because it should be installable in production on a Tuesday afternoon.

Bollard cannot corrupt what an agent sends or receives: it relays every message
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

## Install

```bash
pip install -e .
```

## Use

Wrap any stdio MCP server:

```bash
bollard run --echo -- npx -y @modelcontextprotocol/server-filesystem /data
```

In an MCP client config, replace the server command with the wrapped one:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "bollard",
      "args": [
        "run", "--label", "filesystem", "--",
        "npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"
      ]
    }
  }
}
```

### Remote servers

Production agents mostly talk to remote MCP servers over Streamable HTTP. Put
Bollard in front of one and point the client at the local address instead:

```bash
bollard proxy --upstream https://mcp.example.com/mcp --port 8100 --echo
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
handshake works without Bollard understanding it. Both transports share one
recorder (`CallTracker`), so a row looks the same whichever produced it.

The agent behaves exactly as before. Then look at what it did:

```bash
bollard stats            # per-tool volume, errors, latency, destinations
bollard tail -n 20       # the most recent calls
bollard export out.jsonl # everything, for analysis
```

### Then let it write the rules

The next tier was going to be YAML you write by hand. But a person typing
`max_payload: 8KB` for `send_email` is guessing at a number they have no way to
know — which is the thing this project says the industry is doing wrong.
Enforcement without data is guessing with extra steps, and a rule language is
not data. So the rules come out of the observation tier instead:

```
$ bollard suggest --since 14d

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

### Try it without an agent

```bash
python examples/demo.py
```

## Privacy

| Flag | Effect |
|---|---|
| *(default)* | Credentials in argument values are redacted before storage |
| `--no-redact` | Stores argument values verbatim, credentials included |
| `--no-args` | Stores argument *shape* only (`{"to": "<str:20>"}`), never values |
| `--max-arg-bytes N` | Caps stored bytes; the true size is still recorded |
| `--home DIR` | Where data lives (default `~/.bollard`) |

**Redaction is on by default.** Tool arguments routinely carry API keys, bearer
tokens and connection strings, and without this every install would be a
plaintext credential store that didn't exist before Bollard was installed. Known
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

`events` — `initialize` and `tools/list`, so you know which tools were exposed.

`sessions` — one row per wrapped process, with the exit code.

SQLite at `~/.bollard/bollard.db`, plus an append-only `calls.jsonl`.

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
traffic recorded through Bollard itself.

```bash
python experiments/run.py --driver scripted --out runs/pilot --fresh --repeats 4
python experiments/analyze_runs.py runs/pilot
```

That validates the pipeline with no API key and no network. Swap
`--driver llm` with a Groq free-tier key for the real thing. Protocol and
design are in [experiments/README.md](experiments/README.md).

## Where this is going

1. **Now** — observe. Record every call, block nothing.
2. **Next** — deterministic policy: the rules `bollard suggest` proposes,
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
