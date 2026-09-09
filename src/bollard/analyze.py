"""Signal extraction from tool-call arguments.

Two questions decide whether an action is safe, and both can be answered before
the action happens:

    how much is leaving   ->  byte size of the arguments
    where is it going     ->  the destination the call is addressed to

The second one is easy to get wrong. A naive scan for URLs and email addresses
across the whole argument blob will happily report four hundred customer emails
from inside a message body as "destinations", and bury the single unknown host
the message is actually addressed to.

So we separate them:

    destinations    entities found in routing fields (to, url, webhook, ...)
    content_counts  how many entities appear in the payload

Those are different signals and they compose. "29KB addressed to an unknown
host, containing 400 email addresses" is a shape worth stopping. "29KB
containing 400 email addresses, addressed to the CRM you always use" is a
Tuesday.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

URL_RE = re.compile(r"https?://[^\s\"'<>)\]}]+", re.I)

# The leading lookbehind is load-bearing, not stylistic.
#
# The obvious form -- [\w.+-]+@[\w-]+\.[\w.-]+ -- is quadratic on any long run
# of word characters. The engine matches the whole run with [\w.+-]+, fails to
# find '@', backtracks one character, fails again, and then restarts the same
# walk from every subsequent offset in the run. A 20KB base64 attachment cost
# ~1.8s; 80KB cost ~30s; growth is 4x for every 2x of input.
#
# That is a denial of service reachable by exactly the payload this tool exists
# to notice -- a large body headed somewhere unexpected. Anyone who could
# trigger our alert could first make us spend minutes of CPU not raising it.
#
# (?<![\w.+-]) forbids starting mid-run, so the engine fails immediately at
# every offset after the first instead of re-walking. Linear, ~4000x faster on
# a 20KB blob, and identical results on real addresses -- see
# tests/test_hardening.py, which asserts both the equivalence and the bound.
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,255}\.[\w.-]{1,255}")

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# Hard ceiling on how much text we will scan for entities, per call.
# Defence in depth: the regexes above are linear now, but a pathological input
# should cost a bounded amount regardless of what any future pattern does.
MAX_SCAN_CHARS = 262_144

# Argument names that say where a call is addressed. Matched as substrings, so
# "attach_url", "recipients" and "callbackUrl" are all caught.
ROUTING_TOKENS = (
    "to", "cc", "bcc", "url", "uri", "endpoint", "host", "recipient",
    "dest", "webhook", "callback", "address", "target", "server", "upload",
    "forward", "send_to", "mailto", "channel", "remote",
)

_MAX_PER_KIND = 50
_MAX_DEPTH = 6


def is_routing_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in ("to", "cc", "bcc"):
        return True
    return any(token in lowered for token in ROUTING_TOKENS if len(token) > 2)


def extract_entities(blob: str) -> Dict[str, List[str]]:
    """Scan a string for hosts, emails and IPs. Returns only non-empty kinds.

    Scanning is capped at MAX_SCAN_CHARS. A destination appears in a routing
    field, and routing fields are short; truncation costs us nothing we were
    going to use, and bounds the cost of a hostile payload.
    """
    if len(blob) > MAX_SCAN_CHARS:
        blob = blob[:MAX_SCAN_CHARS]
    hosts = [h for h in (_host_of(u) for u in URL_RE.findall(blob)[:_MAX_PER_KIND]) if h]
    emails = [e.lower() for e in EMAIL_RE.findall(blob)[:_MAX_PER_KIND]]
    ips = [ip for ip in IPV4_RE.findall(blob)[:_MAX_PER_KIND] if _is_ipv4(ip)]

    out: Dict[str, List[str]] = {}
    if hosts:
        out["hosts"] = sorted(set(hosts))
    if emails:
        out["emails"] = sorted(set(emails))
    if ips:
        out["ips"] = sorted(set(ips))
    return out


def extract_signals(args: Any) -> Dict[str, Any]:
    """Split argument entities into destinations and payload content.

    Returns ``{"destinations": {...}, "content_counts": {...}}``, omitting
    either key when it is empty so a call with nothing interesting in it stores
    an empty dict rather than a nest of empty containers.
    """
    routing_blobs: List[str] = []
    content_blobs: List[str] = []
    _walk(args, False, routing_blobs, content_blobs)

    destinations = extract_entities(" ".join(routing_blobs)) if routing_blobs else {}
    content = extract_entities(" ".join(content_blobs)) if content_blobs else {}

    signals: Dict[str, Any] = {}
    if destinations:
        signals["destinations"] = destinations
    if content:
        # counts, not values: we want to know that 400 addresses went out,
        # not to keep a copy of them
        signals["content_counts"] = {k: len(v) for k, v in content.items()}
    return signals


def _walk(node: Any, routing: bool, routing_out: List[str], content_out: List[str],
          depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            _walk(value, routing or is_routing_key(str(key)),
                  routing_out, content_out, depth + 1)
    elif isinstance(node, list):
        for item in node[:200]:
            _walk(item, routing, routing_out, content_out, depth + 1)
    elif isinstance(node, str):
        (routing_out if routing else content_out).append(node)
    elif node is not None and not isinstance(node, bool):
        (routing_out if routing else content_out).append(str(node))


def _host_of(url: str) -> str:
    try:
        rest = url.split("://", 1)[1]
    except IndexError:
        return ""
    authority = rest.split("/", 1)[0].split("?")[0].split("@")[-1]
    host = authority.rsplit(":", 1)[0] if authority.count(":") == 1 else authority
    return host.strip().lower().rstrip(".")


def _is_ipv4(candidate: str) -> bool:
    parts = candidate.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def shape_only(value: Any, depth: int = 0) -> Any:
    """Reduce arguments to their structure, discarding every value.

    Used by --no-args so a team can run Bollard against sensitive traffic and
    still contribute size and destination signal without exposing content.
    """
    if depth > 4:
        return "<deep>"
    if isinstance(value, dict):
        return {k: shape_only(v, depth + 1) for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        return [f"<list:{len(value)}>"] if value else []
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, str):
        return f"<str:{len(value)}>"
    if isinstance(value, (int, float)):
        return "<num>"
    if value is None:
        return None
    return "<other>"


def serialise(args: Any) -> str:
    return json.dumps(args, ensure_ascii=False, default=str)
