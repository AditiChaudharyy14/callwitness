"""Render the public /baseline page from the baseline document itself.

Generated rather than hand-written for one reason: a page that restates numbers
it does not read will eventually disagree with them. Every figure below comes
out of docs/baseline/v1.json, so the prose and the endpoint cannot drift apart
-- and widening the census is one command followed by this one, not an
afternoon of editing HTML.

    python census/baseline.py          # census  -> docs/baseline/v1.json
    python census/render_baseline.py   # v1.json -> docs/baseline/index.html
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List


# -- formatting -----------------------------------------------------------

def human(n: int) -> str:
    """Bytes, at the precision a reader can hold in their head."""
    if n >= 1_000_000:
        return "{:.1f} MB".format(n / 1_000_000)
    if n >= 1_000:
        return "{:.0f} KB".format(n / 1_000)
    return "{} B".format(n)


def ratio(n: float) -> str:
    if n >= 100:
        return "{:,.0f}x".format(n)
    if n >= 10:
        return "{:.0f}x".format(n)
    return "{:.2f}x".format(n)


def short(package: str) -> str:
    """A chart label, not an identifier -- the table keeps the full name.

    The scope is usually noise (@modelcontextprotocol/server-filesystem reads
    fine as server-filesystem) except when the unscoped name is generic:
    @playwright/mcp would become "mcp", which names nothing.
    """
    if not package.startswith("@"):
        return package
    scope, _, name = package[1:].partition("/")
    if len(name) <= 6 or name in ("mcp", "server", "client", "mcp-server"):
        return "{}/{}".format(scope, name)
    return name


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# -- the chart ------------------------------------------------------------

ROW = 27
PAD_TOP = 34
PAD_BOTTOM = 30
LABEL_W = 210
RIGHT = 58
PLOT_W = 470


def log_x(value: int, lo: float, hi: float) -> float:
    """Position on a log axis. Sizes here span four orders of magnitude;
    a linear axis would put fifteen of sixteen servers on the same pixel."""
    v = max(value, 1)
    return LABEL_W + PLOT_W * (math.log10(v) - lo) / (hi - lo)


def chart(pairs: List[Dict[str, Any]]) -> str:
    """Declared against delivered, one row per server, log scale.

    A dumbbell rather than two bar charts: the quantity the reader needs is
    the gap between the two marks on the same row, and a gap is only legible
    when both ends share a scale and a baseline.
    """
    lo, hi = 2.0, 6.0            # 100 B .. 1 MB
    height = PAD_TOP + ROW * len(pairs) + PAD_BOTTOM
    width = LABEL_W + PLOT_W + RIGHT

    out = ['<svg class="chart" viewBox="0 0 {} {}" role="img" '
           'aria-label="Declared size against largest delivered size, '
           'by server, on a logarithmic scale">'.format(width, height)]

    # gridlines, one per decade, labelled with a value the axis reaches
    for decade in range(2, 7):
        x = LABEL_W + PLOT_W * (decade - lo) / (hi - lo)
        out.append('<line class="grid" x1="{0:.1f}" y1="{1}" x2="{0:.1f}" '
                   'y2="{2}" />'.format(x, PAD_TOP - 14, height - PAD_BOTTOM + 6))
        out.append('<text class="tick" x="{:.1f}" y="{}">{}</text>'.format(
            x, height - PAD_BOTTOM + 20, human(10 ** decade)))

    for i, row in enumerate(pairs):
        y = PAD_TOP + ROW * i + ROW / 2
        xd = log_x(row["declared"], lo, hi)
        xr = log_x(row["delivered"], lo, hi)

        out.append('<text class="rowlab" x="{}" y="{:.1f}">{}</text>'.format(
            LABEL_W - 12, y + 4, esc(short(row["package"]))))
        out.append('<line class="link" x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" '
                   'y2="{:.1f}" />'.format(xd, y, xr, y))
        out.append('<g><title>{} declared {} and returned {} ({})</title>'
                   '<circle class="declared" cx="{:.1f}" cy="{:.1f}" r="5" />'
                   '<circle class="delivered" cx="{:.1f}" cy="{:.1f}" r="5" />'
                   '</g>'.format(esc(short(row["package"])),
                                 human(row["declared"]), human(row["delivered"]),
                                 ratio(row["ratio"]), xd, y, xr, y))

        # direct-label only the rows that carry the argument
        if row["flag"]:
            out.append('<text class="callout" x="{:.1f}" y="{:.1f}">{}</text>'
                       .format(xr + 11, y + 4, ratio(row["ratio"])))

    out.append('</svg>')
    return "\n".join(out)


# -- the page -------------------------------------------------------------


def _previous_maxes(path: Path) -> Dict[str, int]:
    """Largest successful response per package in an earlier census run.

    Read straight from the raw JSONL rather than from a rendered document:
    earlier runs predate the baseline format, and there is no reason to
    re-render history in order to compare against it.
    """
    if not path.is_file():
        return {}
    found = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not row.get("started"):
                continue
            sizes = [c.get("returned_bytes") or 0
                     for c in (row.get("calls") or []) if not c.get("is_error")]
            if not sizes:
                continue
            package = row.get("package") or row.get("server") or "?"
            found[package] = max(found.get(package, 0), max(sizes))
    except Exception:
        return {}
    return found


def two_run(servers, previous) -> str:
    """The same server, measured twice, with its declared size unchanged.

    This is the argument the whole page makes, in the one form that cannot be
    waved away as a strange outlier: nothing about the server changed between
    the runs except the argument it was asked.
    """
    if not previous:
        return ""
    moved = []
    for s in servers:
        was = previous.get(s["package"], 0)
        now = s["returned_bytes"]["max"]
        declared = s["declared_bytes"]
        if not (was and now and declared):
            continue
        swing = max(was, now) / min(was, now)
        if swing >= 2:
            moved.append((swing, s["package"], declared, was, now))
    if not moved:
        return ""
    moved.sort(reverse=True)
    swing, package, declared, was, now = moved[0]

    return (
        '<section>'
        '<h2>The same server, measured twice</h2>'
        '<p>Two censuses have been run. <code>' + esc(short(package)) + '</code>'
        ' appears in both, declaring the same ' + human(declared) + ' of schema'
        ' each time, and the size it handed back moved by <b>' + ratio(swing)
        + '</b>:</p>'
        '<div class="tablewrap"><table><thead><tr><th>run</th>'
        '<th class="num">declared</th><th class="num">delivered</th>'
        '<th class="num">ratio</th></tr></thead><tbody>'
        '<tr><td class="pkg">first census</td><td class="num">'
        + human(declared) + '</td><td class="num">' + human(was)
        + '</td><td class="num ratio">' + ratio(was / declared) + '</td></tr>'
        '<tr><td class="pkg">second census</td><td class="num">'
        + human(declared) + '</td><td class="num">' + human(now)
        + '</td><td class="num ratio">' + ratio(now / declared) + '</td></tr>'
        '</tbody></table></div>'
        '<p style="margin-top:1.2rem">Nothing about the server changed. Its'
        ' declared size is a constant; only the argument differed. Whatever an'
        ' install-time number can tell you, it could not have told you this'
        ' &mdash; and no ranking built on declared size survives a '
        + ratio(swing) + ' swing in what actually gets delivered.</p>'
        '<p>Both runs are published, as <code>census/data/census-r1.jsonl</code>'
        ' and <code>census/data/census.jsonl</code>. The comparison above is'
        ' computed from them rather than typed in.</p>'
        '</section>'
    )


def build(doc: Dict[str, Any], previous) -> str:
    servers = doc["servers"]
    sample = doc["sample"]
    overall = doc["overall"]["returned_bytes"]

    pairs = []
    for s in servers:
        if s["declared_bytes"] and s["returned_bytes"]["n"]:
            pairs.append({
                "package": s["package"],
                "declared": s["declared_bytes"],
                "delivered": s["returned_bytes"]["max"],
                "n": s["returned_bytes"]["n"],
                "ratio": s["returned_bytes"]["max"] / s["declared_bytes"],
            })
    pairs.sort(key=lambda p: p["ratio"], reverse=True)
    for p in pairs:
        p["flag"] = p is pairs[0] or p is pairs[-1]

    biggest = pairs[0]
    smallest = pairs[-1]
    declared_only = [s for s in servers
                     if s["declared_bytes"] and not s["returned_bytes"]["n"]]
    loudest = max(servers, key=lambda s: s["declared_bytes"])

    spread = overall["max"] / max(overall["min"], 1)

    rows = "\n".join(
        '<tr><td class="pkg">{}</td><td class="num">{}</td>'
        '<td class="num">{}</td><td class="num ratio">{}</td>'
        '<td class="num n">{}</td></tr>'.format(
            esc(p["package"]), human(p["declared"]),
            human(p["delivered"]), ratio(p["ratio"]), p["n"])
        for p in pairs)

    return PAGE.replace("{{TWORUN}}", two_run(servers, previous)) \
               .replace("{{CHART}}", chart(pairs)) \
               .replace("{{ROWS}}", rows) \
               .replace("{{CALLS}}", str(sample["calls"])) \
               .replace("{{CALLED}}", str(sample["servers_called"])) \
               .replace("{{STARTED}}", str(sample["servers_started"])) \
               .replace("{{TOOLS}}", "{:,}".format(sample["tools_declared"])) \
               .replace("{{CHARTABLE}}", str(len(pairs))) \
               .replace("{{P50}}", human(overall["p50"])) \
               .replace("{{P95}}", human(overall["p95"])) \
               .replace("{{MIN}}", human(overall["min"])) \
               .replace("{{MAX}}", human(overall["max"])) \
               .replace("{{SPREAD}}", "{:,.0f}x".format(spread)) \
               .replace("{{BIG_PKG}}", esc(short(biggest["package"]))) \
               .replace("{{BIG_DECL}}", human(biggest["declared"])) \
               .replace("{{BIG_DELIV}}", human(biggest["delivered"])) \
               .replace("{{BIG_RATIO}}", ratio(biggest["ratio"])) \
               .replace("{{SMALL_PKG}}", esc(short(smallest["package"]))) \
               .replace("{{SMALL_DECL}}", human(smallest["declared"])) \
               .replace("{{SMALL_DELIV}}", human(smallest["delivered"])) \
               .replace("{{LOUD_PKG}}", esc(short(loudest["package"]))) \
               .replace("{{LOUD_DECL}}", human(loudest["declared_bytes"])) \
               .replace("{{UNCALLED}}", str(len(declared_only))) \
               .replace("{{COMMIT}}", esc(doc.get("commit", "")[:7])) \
               .replace("{{GENERATED}}", esc(doc.get("generated_at", "")[:10]))


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>The MCP delivered-size baseline &middot; callwitness</title>
<meta name="description" content="What MCP servers declare at install time, measured against what they actually hand a model at runtime. {{CALLS}} calls, {{CALLED}} servers, raw data published.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,600;1,6..72,400&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root{
  --paper:#F4F5F2; --raised:#FBFBF9; --ink:#16181C; --ink-soft:#4A4A58;
  --ink-faint:#868C96; --rule:#D9DBD6; --rule-soft:#E7E9E4;
  --accent:#2E3BA6; --accent-soft:#E6E8F6;
  --delivered:#4552C9; --declared:#9B4A16;
  --f-display:"Newsreader",Georgia,"Times New Roman",serif;
  --f-body:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  --f-mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --measure:70ch;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#131416; --raised:#1B1D20; --ink:#EDEDEA; --ink-soft:#B2B4BA;
  --ink-faint:#7E838C; --rule:#2E3136; --rule-soft:#24262A;
  --accent:#9AA4F2; --accent-soft:#22263F;
  --delivered:#8E99EE; --declared:#DE9760;
  color-scheme:dark;
}}
:root[data-theme="dark"]{
  --paper:#131416; --raised:#1B1D20; --ink:#EDEDEA; --ink-soft:#B2B4BA;
  --ink-faint:#7E838C; --rule:#2E3136; --rule-soft:#24262A;
  --accent:#9AA4F2; --accent-soft:#22263F;
  --delivered:#8E99EE; --declared:#DE9760;
  color-scheme:dark;
}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);font-family:var(--f-body);
  font-size:16px;line-height:1.62;margin:0;padding-inline:20px;padding-block:0;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:var(--measure);margin:0 auto;padding-block:clamp(2.4rem,6vw,4.5rem) 5rem}
a{color:var(--accent);text-underline-offset:2px;text-decoration-thickness:1px}
code{font-family:var(--f-mono);font-size:.855em;background:var(--rule-soft);
  padding:.1em .35em;border-radius:3px}
p{margin:0 0 1.05rem}

.crumb{font-family:var(--f-mono);font-size:.72rem;letter-spacing:.11em;
  text-transform:uppercase;color:var(--ink-faint);margin:0 0 1.1rem}
h1{font-family:var(--f-display);font-weight:600;font-size:clamp(2rem,5.4vw,2.85rem);
  line-height:1.1;letter-spacing:-.015em;margin:0;text-wrap:balance}
.stand{font-family:var(--f-display);font-size:1.16rem;line-height:1.52;
  color:var(--ink-soft);margin:1rem 0 0;max-width:58ch}
.meta{display:flex;flex-wrap:wrap;gap:.4rem 1.4rem;margin-top:1.6rem;
  padding-top:1.1rem;border-top:2px solid var(--ink);
  font-family:var(--f-mono);font-size:.72rem;letter-spacing:.05em;color:var(--ink-faint)}
.meta span{white-space:nowrap}

section{margin-top:3.2rem}
h2{font-family:var(--f-display);font-weight:600;font-size:1.5rem;line-height:1.22;
  margin:0 0 1rem;letter-spacing:-.01em;text-wrap:balance}
h3{font-family:var(--f-body);font-weight:600;font-size:1.02rem;margin:1.8rem 0 .4rem}

.lede{font-family:var(--f-display);font-size:1.3rem;line-height:1.48;
  margin:0 0 1.4rem;color:var(--ink)}
.lede b{font-weight:600;color:var(--declared)}

figure{margin:1.6rem 0 0}
.chart-card{border:1px solid var(--rule);background:var(--raised);
  border-radius:2px;padding:1.1rem .9rem .6rem;overflow-x:auto}
.chart{width:100%;min-width:620px;height:auto;display:block}
.grid{stroke:var(--rule);stroke-width:1}
.tick{font-family:var(--f-mono);font-size:10px;fill:var(--ink-faint);text-anchor:middle}
.rowlab{font-family:var(--f-mono);font-size:11px;fill:var(--ink-soft);text-anchor:end}
.link{stroke:var(--rule);stroke-width:2}
.declared{fill:var(--declared);stroke:var(--raised);stroke-width:2}
.delivered{fill:var(--delivered);stroke:var(--raised);stroke-width:2}
.callout{font-family:var(--f-mono);font-size:11px;font-weight:500;fill:var(--ink)}
.legend{display:flex;flex-wrap:wrap;gap:.4rem 1.5rem;margin:.9rem 0 0;
  font-family:var(--f-mono);font-size:.72rem;color:var(--ink-soft)}
.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;
  margin-right:.45rem;vertical-align:baseline}
.legend .d1{background:var(--declared)}
.legend .d2{background:var(--delivered)}
figcaption{font-size:.88rem;color:var(--ink-faint);margin-top:.9rem;line-height:1.55}

.tablewrap{overflow-x:auto;border:1px solid var(--rule);border-radius:2px;
  background:var(--raised);margin-top:1.5rem}
table{border-collapse:collapse;width:100%;font-size:.87rem;min-width:480px}
th{font-family:var(--f-mono);font-size:.67rem;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-faint);text-align:left;
  padding:.7rem .8rem;border-bottom:1px solid var(--rule);font-weight:400;
  white-space:nowrap}
td{padding:.5rem .8rem;border-bottom:1px solid var(--rule-soft)}
tr:last-child td{border-bottom:0}
.pkg{font-family:var(--f-mono);font-size:.79rem;color:var(--ink)}
.num{font-family:var(--f-mono);font-variant-numeric:tabular-nums;
  text-align:right;color:var(--ink-soft);white-space:nowrap}
.ratio{color:var(--ink);font-weight:500}
.n{color:var(--ink-faint)}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:1px;background:var(--rule);border:1px solid var(--rule);
  border-radius:2px;margin-top:1.5rem;overflow:hidden}
.stat{background:var(--raised);padding:1rem 1.05rem}
.stat-v{font-family:var(--f-mono);font-size:1.4rem;font-weight:500;
  line-height:1.15;font-variant-numeric:tabular-nums}
.stat-k{font-family:var(--f-mono);font-size:.66rem;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-faint);margin-top:.4rem}

.note{border:1px solid var(--rule);border-left:3px solid var(--declared);
  background:var(--raised);border-radius:2px;padding:1.15rem 1.25rem;margin-top:1.5rem}
.note p{font-size:.94rem;color:var(--ink-soft);margin:0 0 .8rem}
.note p:last-child{margin:0}
.note b{color:var(--ink)}

pre{font-family:var(--f-mono);font-size:.82rem;line-height:1.65;
  background:var(--raised);border:1px solid var(--rule);border-radius:2px;
  padding:.9rem 1rem;overflow-x:auto;margin:1rem 0}
pre code{background:none;padding:0;font-size:1em}

footer{margin-top:3.6rem;padding-top:1.2rem;border-top:1px solid var(--rule);
  font-family:var(--f-mono);font-size:.72rem;line-height:1.8;color:var(--ink-faint)}
footer a{color:var(--ink-soft)}
</style>
</head>
<body>
<div class="wrap">

<p class="crumb"><a href="/">callwitness</a> &nbsp;/&nbsp; baseline</p>

<h1>What MCP servers actually return</h1>
<p class="stand">Every published measurement of MCP context cost counts the schemas a server declares at install time. Nobody counts what it hands the model at runtime, because nobody runs the servers. This is that measurement.</p>

<div class="meta">
  <span>{{CALLS}} calls</span>
  <span>{{CALLED}} of {{STARTED}} servers</span>
  <span>{{TOOLS}} tools declared</span>
  <span>generated {{GENERATED}}</span>
  <span>commit {{COMMIT}}</span>
</div>

<section>
  <p class="lede">Declared size does not predict delivered size. Not loosely &mdash; <b>not at all</b>.</p>
  <p><code>{{BIG_PKG}}</code> declares {{BIG_DECL}} of schema, close to the smallest menu in the census, and returned {{BIG_DELIV}} in a single call &mdash; {{BIG_RATIO}} its declared size. <code>{{SMALL_PKG}}</code> declares {{SMALL_DECL}} and returned {{SMALL_DELIV}}. Ranked by what they declare, these two servers sit at opposite ends of the table from where they land when ranked by what they deliver.</p>
  <p>The install-time number, in other words, tells you almost nothing about the runtime cost &mdash; and the install-time number is the only one anyone currently publishes.</p>
</section>

<section>
  <h2>Declared against delivered</h2>
  <figure>
    <div class="chart-card">
      {{CHART}}
    </div>
    <div class="legend">
      <span><i class="d1"></i>declared at install time (<code>tools/list</code>)</span>
      <span><i class="d2"></i>largest single response observed</span>
    </div>
    <figcaption>{{CHARTABLE}} servers where both halves were measured, ordered by ratio. Logarithmic scale &mdash; the delivered values span four orders of magnitude, and a linear axis would stack fifteen of these rows on one pixel. Hover any pair for its numbers.</figcaption>
  </figure>
</section>

<section>
  <h2>The distribution</h2>
  <div class="stats">
    <div class="stat"><div class="stat-v">{{P50}}</div><div class="stat-k">median response</div></div>
    <div class="stat"><div class="stat-v">{{P95}}</div><div class="stat-k">95th percentile</div></div>
    <div class="stat"><div class="stat-v">{{MAX}}</div><div class="stat-k">largest observed</div></div>
    <div class="stat"><div class="stat-v">{{SPREAD}}</div><div class="stat-k">min to max spread</div></div>
  </div>
  <p style="margin-top:1.5rem">A typical tool response is {{P50}}. One in twenty is {{P95}} or larger. The largest single response measured was {{MAX}}, from {{BIG_DECL}} of declared schema and a handful of bytes of argument.</p>
  <p>This is why a fixed context budget behaves unpredictably in practice: the cut point is static, and the pressure on it varies by {{SPREAD}} depending on which argument the model happens to pick at runtime.</p>
</section>

{{TWORUN}}
<section>
  <h2>Every measured server</h2>
  <div class="tablewrap">
    <table>
      <thead><tr>
        <th>package</th><th class="num">declared</th>
        <th class="num">largest returned</th><th class="num">ratio</th><th class="num">calls</th>
      </tr></thead>
      <tbody>
{{ROWS}}
      </tbody>
    </table>
  </div>
  <p style="margin-top:1rem;font-size:.92rem;color:var(--ink-faint)">{{UNCALLED}} further servers declared a menu but exposed no tool that could be called safely without side effects, so they have a declared size and no delivered one. They are in the raw data with their declared sizes intact.</p>
</section>

<section>
  <h2>What this is not</h2>
  <div class="note">
    <p><b>{{CALLS}} calls across {{CALLED}} servers, on one machine.</b> That is a small sample, and it is the weakest thing about this work. I could not find another published measurement of delivered size, which makes this the best number available and still a thin one. If one exists, I would rather cite it than be the only source.</p>
    <p>Every figure here carries its <code>n</code>. If you take a number from this page, take the <code>n</code> with it.</p>
    <p>The servers were exercised with read-only tools chosen by an allowlist, one request at a time. Nothing was written, deleted or executed. Servers that refused without credentials, and servers that would not start, are published as part of the dataset rather than dropped from it &mdash; a census that silently excludes its failures is not a census.</p>
  </div>
</section>

<section>
  <h2>Use it</h2>
  <p>The distribution is published as a versioned document, not a number in a blog post. Fetch it rather than copying constants, and it improves without you editing anything:</p>
  <pre><code>https://callwitness.tech/baseline/v1.json</code></pre>
  <p>Inside <code>callwitness.baseline.v1</code>, field names, units and meanings will not change. A breaking change becomes v2 at a new path, and v1 keeps resolving. Every server carries its per-call sizes, not only the aggregate, and <code>returned_bytes_all</code> holds every delivered size in one sorted list so any percentile is a line of code away.</p>

  <h3>Measure your own servers</h3>
  <p>These are someone else's servers. Yours will differ, and the numbers that matter for your context budget are yours:</p>
  <pre><code>pip install callwitness
callwitness run --label docs -- npx -y &lt;your mcp server&gt;
callwitness baseline --out mine.json</code></pre>
  <p>That produces the same schema, from your traffic. Anything reading the public baseline reads your file unchanged &mdash; the only field to branch on is <code>origin</code>, which is <code>"census"</code> in the published document and <code>"local"</code> in yours.</p>

  <h3>Make the baseline less thin</h3>
  <p>The sample above is one person's afternoon. <code>callwitness contribute</code> sends the shape of your traffic &mdash; sizes, counts and durations, never arguments, paths, hostnames or results &mdash; to widen it. It is off unless you turn it on, nothing is sent while the proxy is running, and <code>--dry-run</code> prints the exact bytes that would leave.</p>
</section>

<footer>
  <a href="https://github.com/AditiChaudharyy14/callwitness">source and raw data</a> &nbsp;&middot;&nbsp;
  <a href="https://callwitness.tech/baseline/v1.json">baseline/v1.json</a> &nbsp;&middot;&nbsp;
  MIT<br>
  generated from the dataset at commit {{COMMIT}} &mdash; this page is rendered from
  v1.json, so it cannot disagree with the endpoint
</footer>

</div>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="docs/baseline/v1.json")
    parser.add_argument("--out", default="docs/baseline/index.html")
    parser.add_argument("--previous",
                        default="census/data/census-r1.jsonl",
                        help="an earlier run, for the two-run comparison")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_file():
        print("no baseline at {} -- run census/baseline.py first".format(source))
        return 1

    doc = json.loads(source.read_text(encoding="utf-8"))
    page = build(doc, _previous_maxes(Path(args.previous)))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print("wrote {} ({:,} bytes)".format(out, len(page.encode())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())