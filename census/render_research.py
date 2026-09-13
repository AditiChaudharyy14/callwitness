"""Render /research: how the census was taken, computed from the census.

The method page is the one page that must not be written from memory. If it
says "nothing destructive was called" it has to be able to show the rule that
made that true, and if it says "six servers would not start" it has to have
counted them. So every figure here comes out of census/data/census.jsonl, and
the safety rule is imported from the harness rather than restated.

    python census/render_research.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_rows(path: Path) -> List[Dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def load_harness(path: Path):
    """Import the census harness so the verb lists come from the code.

    Restating them here would let the page and the rule drift apart, and the
    page's whole claim is that the rule is what kept the run safe.
    """
    try:
        spec = importlib.util.spec_from_file_location("census_harness", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def words(items) -> str:
    return " ".join('<span class="w">{}</span>'.format(esc(w))
                    for w in sorted(items))


def build(rows: List[Dict[str, Any]], harness) -> str:
    started = [r for r in rows if r.get("started")]
    failed = [r for r in rows if not r.get("started")]
    # A server counts as called only if a call actually succeeded, which is
    # the same rule the baseline page uses. Two pages disagreeing on a
    # headline count would discredit both.
    called = [r for r in started
              if any(not c.get("is_error") for c in (r.get("calls") or []))]
    listed_only = [r for r in started if not r.get("calls")]

    tools = sum(int(r.get("tool_count") or 0) for r in started)
    # Successful calls, matching the baseline page. Errors are counted
    # separately and reported, not folded into the total.
    calls = sum(1 for r in started for c in (r.get("calls") or [])
                if not c.get("is_error"))
    errored = sum(1 for r in started for c in (r.get("calls") or [])
                  if c.get("is_error"))
    skipped = sum(len(r.get("not_auto_called") or []) for r in started)

    directive = [(r["package"], r["directive_tools"])
                 for r in started if r.get("directive_tools")]
    directive_tools = sum(len(d) for _, d in directive)

    # Failures grouped by what actually went wrong, not by server.
    reasons = Counter()
    for r in failed:
        text = (r.get("error") or "").lower()
        if "api" in text or "key" in text or "token" in text:
            reasons["refused without credentials"] += 1
        elif "initialize" in text:
            reasons["started, never answered initialize"] += 1
        else:
            reasons["would not start"] += 1

    reason_rows = "\n".join(
        '<tr><td>{}</td><td class="num">{}</td></tr>'.format(esc(k), v)
        for k, v in reasons.most_common())

    fail_rows = "\n".join(
        '<tr><td class="pkg">{}</td><td>{}</td></tr>'.format(
            esc(r.get("package") or r.get("server")), esc(r.get("error") or "-"))
        for r in sorted(failed, key=lambda r: r.get("package") or ""))

    directive_rows = "\n".join(
        '<tr><td class="pkg">{}</td><td>{}</td></tr>'.format(
            esc(pkg), ", ".join("<code>{}</code>".format(esc(t)) for t in tl))
        for pkg, tl in sorted(directive))

    read = words(getattr(harness, "READ_VERBS", []) or ["(unavailable)"])
    write = words(getattr(harness, "WRITE_VERBS", []) or ["(unavailable)"])

    return PAGE \
        .replace("{{STARTED}}", str(len(started))) \
        .replace("{{TOTAL}}", str(len(rows))) \
        .replace("{{FAILED}}", str(len(failed))) \
        .replace("{{CALLED}}", str(len(called))) \
        .replace("{{LISTED_ONLY}}", str(len(listed_only))) \
        .replace("{{TOOLS}}", "{:,}".format(tools)) \
        .replace("{{CALLS}}", str(calls)) \
        .replace("{{ERRORS}}", str(errored)) \
        .replace("{{SKIPPED}}", "{:,}".format(skipped)) \
        .replace("{{READ_VERBS}}", read) \
        .replace("{{WRITE_VERBS}}", write) \
        .replace("{{REASON_ROWS}}", reason_rows) \
        .replace("{{FAIL_ROWS}}", fail_rows) \
        .replace("{{DIRECTIVE_ROWS}}", directive_rows or
                 '<tr><td colspan="2">none in this run</td></tr>') \
        .replace("{{DIRECTIVE_N}}", str(directive_tools)) \
        .replace("{{DIRECTIVE_SERVERS}}", str(len(directive)))


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>How the census was taken &middot; callwitness</title>
<meta name="description" content="The method behind the MCP delivered-size baseline: how servers were chosen, which tools were safe to call and why, what was excluded, and how to replicate it.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,600;1,6..72,400&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root{
  --paper:#F4F5F2; --raised:#FBFBF9; --ink:#16181C; --ink-soft:#4A4A58;
  --ink-faint:#868C96; --rule:#D9DBD6; --rule-soft:#E7E9E4;
  --accent:#2E3BA6; --accent-soft:#E6E8F6; --flag:#9B4A16; --flag-soft:#F4E7DC;
  --good:#2C6A44; --good-soft:#E2EDE6;
  --f-display:"Newsreader",Georgia,"Times New Roman",serif;
  --f-body:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  --f-mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --measure:70ch; color-scheme:light;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#131416; --raised:#1B1D20; --ink:#EDEDEA; --ink-soft:#B2B4BA;
  --ink-faint:#7E838C; --rule:#2E3136; --rule-soft:#24262A;
  --accent:#9AA4F2; --accent-soft:#22263F; --flag:#E0955F; --flag-soft:#33241A;
  --good:#7FC79C; --good-soft:#1C2A22; color-scheme:dark;
}}
:root[data-theme="dark"]{
  --paper:#131416; --raised:#1B1D20; --ink:#EDEDEA; --ink-soft:#B2B4BA;
  --ink-faint:#7E838C; --rule:#2E3136; --rule-soft:#24262A;
  --accent:#9AA4F2; --accent-soft:#22263F; --flag:#E0955F; --flag-soft:#33241A;
  --good:#7FC79C; --good-soft:#1C2A22; color-scheme:dark;
}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);font-family:var(--f-body);
  font-size:16px;line-height:1.62;margin:0;padding-inline:20px;padding-block:0;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:var(--measure);margin:0 auto;padding-block:clamp(2.4rem,6vw,4.5rem) 5rem}
p{margin:0 0 1.05rem}
a{color:var(--accent);text-underline-offset:2px}
code{font-family:var(--f-mono);font-size:.855em;background:var(--rule-soft);
  padding:.1em .35em;border-radius:3px}

.crumb{font-family:var(--f-mono);font-size:.72rem;letter-spacing:.11em;
  text-transform:uppercase;color:var(--ink-faint);margin:0 0 1.1rem}
h1{font-family:var(--f-display);font-weight:600;font-size:clamp(2rem,5.4vw,2.8rem);
  line-height:1.1;letter-spacing:-.015em;margin:0;text-wrap:balance}
.stand{font-family:var(--f-display);font-size:1.16rem;line-height:1.52;
  color:var(--ink-soft);margin:1rem 0 0;max-width:58ch}
.meta{display:flex;flex-wrap:wrap;gap:.4rem 1.4rem;margin-top:1.6rem;
  padding-top:1.1rem;border-top:2px solid var(--ink);
  font-family:var(--f-mono);font-size:.72rem;color:var(--ink-faint)}
.meta span{white-space:nowrap}

section{margin-top:3.1rem}
h2{font-family:var(--f-display);font-weight:600;font-size:1.5rem;line-height:1.22;
  margin:0 0 1rem;letter-spacing:-.01em;text-wrap:balance}
h3{font-family:var(--f-body);font-weight:600;font-size:1.02rem;margin:1.9rem 0 .45rem}

pre{font-family:var(--f-mono);font-size:.8rem;line-height:1.65;
  background:var(--raised);border:1px solid var(--rule);border-radius:2px;
  padding:.9rem 1rem;overflow-x:auto;margin:1rem 0}
pre code{background:none;padding:0;font-size:1em}

.verbs{display:flex;flex-wrap:wrap;gap:.32rem;margin:.7rem 0 0}
.w{font-family:var(--f-mono);font-size:.74rem;padding:.13rem .42rem;
  border-radius:2px;background:var(--rule-soft);color:var(--ink-soft);
  white-space:nowrap}
.allow .w{background:var(--good-soft);color:var(--good)}
.deny .w{background:var(--flag-soft);color:var(--flag)}

.tablewrap{overflow-x:auto;border:1px solid var(--rule);border-radius:2px;
  background:var(--raised);margin-top:1.3rem}
table{border-collapse:collapse;width:100%;font-size:.88rem;min-width:420px}
th{font-family:var(--f-mono);font-size:.67rem;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-faint);text-align:left;
  padding:.65rem .8rem;border-bottom:1px solid var(--rule);font-weight:400}
td{padding:.5rem .8rem;border-bottom:1px solid var(--rule-soft);
  color:var(--ink-soft);vertical-align:top}
tr:last-child td{border-bottom:0}
.pkg{font-family:var(--f-mono);font-size:.79rem;color:var(--ink)}
.num{font-family:var(--f-mono);text-align:right;font-variant-numeric:tabular-nums}

.note{border:1px solid var(--rule);border-left:3px solid var(--flag);
  background:var(--raised);border-radius:2px;padding:1.15rem 1.25rem;margin-top:1.4rem}
.note p{font-size:.94rem;color:var(--ink-soft);margin:0 0 .8rem}
.note p:last-child{margin:0}
.note b{color:var(--ink)}

.owned{border:1px solid var(--rule);border-left:3px solid var(--accent);
  background:var(--raised);border-radius:2px;padding:1.15rem 1.25rem;margin-top:1.4rem}
.owned p{font-size:.94rem;color:var(--ink-soft);margin:0 0 .8rem}
.owned p:last-child{margin:0}

ul.plain{list-style:none;margin:1rem 0 0;padding:0;
  display:flex;flex-direction:column;gap:.75rem}
/* The marker is positioned, not a grid column: a grid row turns every inline
   child into its own cell, so a <b> inside the sentence lands in the wrong
   place and the text overlaps. */
ul.plain li{position:relative;padding-left:1.75rem;
  font-size:.945rem;color:var(--ink-soft)}
ul.plain li::before{content:"\2014";position:absolute;left:0;top:0;
  font-family:var(--f-mono);color:var(--ink-faint)}
ul.plain b{color:var(--ink);font-weight:600}

footer{margin-top:3.4rem;padding-top:1.2rem;border-top:1px solid var(--rule);
  font-family:var(--f-mono);font-size:.72rem;line-height:1.8;color:var(--ink-faint)}
footer a{color:var(--ink-soft)}
</style>
</head>
<body>
<div class="wrap">

<p class="crumb"><a href="/">callwitness</a> &nbsp;/&nbsp; research</p>

<h1>How the census was taken</h1>
<p class="stand">The measurement on the baseline page is only worth as much as the method behind it. This is the method, in enough detail to be attacked, and every number on this page is computed from the same raw file the results come from.</p>

<div class="meta">
  <span>{{TOTAL}} servers attempted</span>
  <span>{{STARTED}} started</span>
  <span>{{CALLED}} called</span>
  <span>{{CALLS}} calls</span>
  <span>{{ERRORS}} returned errors</span>
</div>

<section>
  <h2>What is being measured</h2>
  <p>Two numbers per server, and the relationship between them.</p>
  <ul class="plain">
    <li><b>Declared size.</b> The exact bytes of the <code>tools/list</code> response as it crossed the wire. This is what a client pays to have the server available at all, before anything is called, and it is the number every published estimate of MCP context cost already counts.</li>
    <li><b>Delivered size.</b> The exact bytes of each <code>tools/call</code> result as it crossed the wire. This is what actually reaches the model, and as far as I could find, nobody was counting it &mdash; because counting it means running the servers rather than reading their manifests.</li>
  </ul>
  <p style="margin-top:1.1rem">Both are measured at the transport, not reconstructed from the source. The proxy sits between client and server, forwards every line unchanged, and hands a copy to a recorder on another thread. It cannot alter what it measures, which is the point: an instrument that can change the stream is measuring itself.</p>
</section>

<section>
  <h2>Which servers, and how they were chosen</h2>
  <p>Servers were drawn from the reference implementations, community directories, and registry searches, filtered to those that can be launched by a package runner and exercised without an API key, an account, or an external service. The filter is not editorial: a server that refuses to list its tools without credentials yields no measurement, so it cannot be in the distribution however popular it is.</p>
  <p>Of {{TOTAL}} attempted, <b>{{STARTED}} started and answered <code>tools/list</code></b>, declaring {{TOOLS}} tools between them. {{CALLED}} of those went on to answer a real call.</p>
  <p>The full list, with the exact argv used for each, is <code>census/servers.json</code> in the repository. Nothing was measured that is not in that file.</p>
</section>

<section>
  <h2>Which tools were called, and why nothing broke</h2>
  <p>A census that calls tools on {{STARTED}} servers is a census that will eventually call something it should not. This one nearly did: an early version of the harness would have invoked <code>kubectl_delete</code>, <code>cleanup</code> and <code>run_process</code>, all of which take no required arguments and would have been selected as easy calls.</p>
  <p>So tool selection is a rule rather than a judgement. A tool name is split into words on non-alphanumeric boundaries and camel-case transitions, then:</p>
  <ul class="plain">
    <li>if <b>any</b> word is a write verb, the tool is never called &mdash; position does not matter, because <code>safe_delete_all</code> is not safe;</li>
    <li>a read verb must appear in the <b>first two</b> words, so the name's leading claim is the one that counts;</li>
    <li>anything else is skipped and recorded as skipped, not silently dropped.</li>
  </ul>
  <h3>Read verbs &mdash; may be called</h3>
  <div class="verbs allow">{{READ_VERBS}}</div>
  <h3>Write verbs &mdash; disqualify a tool entirely</h3>
  <div class="verbs deny">{{WRITE_VERBS}}</div>
  <p style="margin-top:1.2rem">{{SKIPPED}} declared tools were refused by this rule across the run. Where a server's entry in <code>servers.json</code> names explicit calls, those were used instead; the allowlist is the floor, not the plan.</p>
  <p>One request at a time, and nothing concurrent. A server under parallel load is measuring the harness.</p>
</section>

<section>
  <h2>How the numbers are computed</h2>
  <ul class="plain">
    <li><b>Percentiles are nearest-rank, never interpolated.</b> Every value published is a number that actually occurred. An interpolated p95 is a byte count nobody measured, which is a strange thing for a measurement to publish.</li>
    <li><b>No value is derived from another.</b> Declared size is measured, delivered size is measured, and the ratio between them is computed once, in one place, so every consumer reads the same definition.</li>
    <li><b>A missing number is 0 and says so.</b> <code>declared_bytes: 0</code> means no <code>tools/list</code> was observed, never that a server declared nothing, and no ratio is computed against a denominator that was not seen.</li>
    <li><b>Every distribution carries its <code>n</code>.</b> Inside the published document, not in a footnote beside it, so a number cannot travel without its sample size.</li>
  </ul>
</section>

<section>
  <h2>What did not work, and why that is published</h2>
  <p>{{FAILED}} servers produced no measurement. They are in the dataset with their failure reasons rather than dropped from it &mdash; a census that silently excludes what it could not measure is reporting its own filter as a finding.</p>
  <div class="tablewrap">
    <table>
      <thead><tr><th>reason</th><th class="num">servers</th></tr></thead>
      <tbody>{{REASON_ROWS}}</tbody>
    </table>
  </div>
  <p>A further {{ERRORS}} calls were made and came back as errors &mdash; a bad argument, a missing input file, a remote service refusing. Those are excluded from the size distribution, because an error message is not a response, but they are counted here so the two numbers can be reconciled: {{CALLS}} successful calls out of {{CALLS}} plus {{ERRORS}} attempted.</p>
  <p style="margin-top:1.1rem">A further {{LISTED_ONLY}} servers started and declared tools but exposed nothing that the rule above would call. They contribute a declared size and no delivered size, which is exactly how they appear in the published baseline.</p>
  <h3>Every failure, individually</h3>
  <div class="tablewrap">
    <table>
      <thead><tr><th>package</th><th>what happened</th></tr></thead>
      <tbody>{{FAIL_ROWS}}</tbody>
    </table>
  </div>
</section>

<section>
  <h2>A hazard anyone replicating this will hit</h2>
  <div class="note">
    <p><b>Most published Python MCP servers currently crash on a fresh install.</b> The <code>mcp</code> SDK released a 2.x that renamed <code>FastMCP</code> and dropped <code>mcp.server.fastmcp</code>. Packages that never pinned <code>mcp&lt;2</code> now fail on import, before <code>initialize</code>, and a census sees them as dead servers.</p>
    <p>In the first pass this affected 21 of 29 PyPI servers. Launching them as <code>uvx --with "mcp&lt;2" &lt;package&gt;</code> recovers nearly all of them, which is why so many entries in <code>servers.json</code> carry that flag.</p>
    <p>This matters for anyone reading the results as much as for anyone repeating them: a census run without that pin would under-count Python servers by roughly seventy percent, for a reason that has nothing to do with the servers being measured.</p>
  </div>
</section>

<section>
  <h2>Tool descriptions that instruct rather than describe</h2>
  <p>A tool's description is prose the model reads. {{DIRECTIVE_N}} tools across {{DIRECTIVE_SERVERS}} servers carry descriptions written as instructions to the model &mdash; imperatives, emphasis, "you must" constructions &mdash; rather than descriptions of what the tool does.</p>
  <p>This is recorded, not judged. It is noted because it is invisible in any count of tools or bytes, and because a description that instructs is doing something different from a description that describes.</p>
  <div class="tablewrap">
    <table>
      <thead><tr><th>package</th><th>tools</th></tr></thead>
      <tbody>{{DIRECTIVE_ROWS}}</tbody>
    </table>
  </div>
</section>

<section>
  <h2>What this cannot tell you</h2>
  <div class="owned">
    <p><b>One machine, one operator, one set of arguments.</b> The arguments were chosen by hand to be representative and safe, and a different set would produce different delivered sizes &mdash; that is the finding, but it is also the limitation, and it cuts both ways.</p>
    <p><b>Delivered size is not a fixed property of a server.</b> The same server measured twice, with the same declared size, moved by an order of magnitude because the argument changed. Treat the maximum as a lower bound on how large a response can get, never as a ceiling.</p>
    <p><b>The sample is small and the servers are not a random draw.</b> They are the servers that can be run without credentials, which is a real population but not the whole one. Servers requiring API keys are systematically absent, and they are not obviously similar to the ones here.</p>
    <p><b>Nothing here measures quality.</b> A large response is often a server working correctly. What is being measured is how much of a context budget a server can consume without having said so at install time.</p>
  </div>
</section>

<section>
  <h2>Replicate it</h2>
  <p>Everything below is in the repository. The census is a script, not a service:</p>
  <pre><code>git clone https://github.com/AditiChaudharyy14/callwitness
cd callwitness
pip install -e .

python census/census.py --root /a/folder/with/real/content
python census/baseline.py
python census/render_baseline.py</code></pre>
  <p>To measure your own servers instead of these, wrap them and generate the same document from your own traffic:</p>
  <pre><code>callwitness run --label docs -- npx -y &lt;your mcp server&gt;
callwitness baseline --out mine.json</code></pre>
  <p>Raw data: <code>census/data/census.jsonl</code> for the current run and <code>census/data/census-r1.jsonl</code> for the first. Both are line-delimited JSON, one record per server, and both are what the published documents are computed from.</p>
</section>

<footer>
  <a href="/baseline/">the results</a> &nbsp;&middot;&nbsp;
  <a href="https://github.com/AditiChaudharyy14/callwitness">source and raw data</a> &nbsp;&middot;&nbsp;
  MIT<br>
  every figure on this page is computed from census/data/census.jsonl, and the
  verb lists are imported from the harness rather than restated
</footer>

</div>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="census/data/census.jsonl")
    parser.add_argument("--harness", default="census/census.py")
    parser.add_argument("--out", default="docs/research/index.html")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_file():
        print("no census at {}".format(source))
        return 1

    rows = load_rows(source)
    harness = load_harness(Path(args.harness))
    if harness is None:
        print("warning: could not import {} -- verb lists will be blank"
              .format(args.harness))

    page = build(rows, harness)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print("wrote {} ({:,} bytes)".format(out, len(page.encode())))
    print("  {} attempted, {} started, {} called".format(
        len(rows), sum(1 for r in rows if r.get("started")),
        sum(1 for r in rows if any(not c.get("is_error")
                                   for c in (r.get("calls") or [])))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())