"""Say exactly what happened to all 86, not just the 65 that answered.

    python census_breakdown.py

"65 of 86 answered" invites one question and has no answer ready. This sorts
every record into a reason and prints a paragraph you can paste into the
README and the research page.

The categories are mechanical, read off the data rather than decided:

  answered        at least one call came back without an error
  no handshake    the server never got as far as declaring tools
  nothing asked   it started and declared tools, but the catalogue defines no
                  call for it -- the census never tried, which is a gap in the
                  catalogue and not a fact about the server
  call failed     a call was defined, was made, and errored

Servers needing credentials and servers whose tools only execute or write are
labelled from a list at the top of this file, because no field in the data
says so. Read that list and correct it -- it is a judgement, and it should be
yours rather than mine.
"""

import io
import json
import sys
from pathlib import Path

CENSUS = Path("census/data/census.jsonl")
CATALOGUE = Path("census/servers.json")

# Judgements, not data. Correct these if you disagree.
NEEDS_CREDENTIALS = {
    "@modelcontextprotocol/server-github",
    "@modelcontextprotocol/server-postgres",
    "@modelcontextprotocol/server-aws-kb-retrieval",
    "@notionhq/notion-mcp-server",
    "firecrawl-mcp",
    "tavily-mcp",
    "exa-mcp-server",
    "mcp-server-kubernetes",
}
EXECUTES = {
    "mcp-server-commands",
    "mcp-server-code-runner",
}


def load_jsonl(path):
    rows = []
    for line in io.open(str(path), encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def name_of(record):
    for key in ("package", "server", "name"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "?"


def entries_of(catalogue):
    if isinstance(catalogue, list):
        return catalogue
    if isinstance(catalogue, dict):
        for key, value in catalogue.items():
            if (isinstance(value, list) and not key.startswith("_")
                    and value and isinstance(value[0], dict)):
                return value
    return []


def main(argv):
    if not CENSUS.is_file() or not CATALOGUE.is_file():
        print("Run this from the repo root.")
        return 1

    records = load_jsonl(CENSUS)
    catalogue = {e.get("package") or e.get("name"): e
                 for e in entries_of(json.loads(
                     io.open(str(CATALOGUE), encoding="utf-8").read()))}

    answered, no_handshake, nothing_asked, call_failed = [], [], [], []
    failures = []

    for record in records:
        name = name_of(record)
        calls = record.get("calls") or []
        good = [c for c in calls if isinstance(c, dict) and not c.get("is_error")]
        defined = len((catalogue.get(name) or {}).get("calls") or [])

        if good:
            answered.append(name)
        elif not record.get("started") and record.get("error"):
            no_handshake.append((name, str(record.get("error"))))
        elif defined == 0:
            nothing_asked.append(name)
        else:
            call_failed.append(name)
            if calls:
                failures.append((name, calls[0]))

    credentials = [n for n in nothing_asked if n in NEEDS_CREDENTIALS]
    executes = [n for n in nothing_asked if n in EXECUTES]
    gap = [n for n in nothing_asked
           if n not in NEEDS_CREDENTIALS and n not in EXECUTES]

    total = len(records)
    print("")
    print("  {} records".format(total))
    print("")
    print("  {:>4}  answered a real call".format(len(answered)))
    print("  {:>4}  never completed a handshake".format(len(no_handshake)))
    print("  {:>4}  need credentials or infrastructure I don't have"
          .format(len(credentials)))
    print("  {:>4}  declare only tools that execute or write".format(len(executes)))
    print("  {:>4}  started fine, but no call was ever defined for them"
          .format(len(gap)))
    print("  {:>4}  had a call defined, made it, and it errored".format(len(call_failed)))
    print("  {:>4}  total".format(len(answered) + len(no_handshake) + len(credentials)
                                  + len(executes) + len(gap) + len(call_failed)))
    print("")

    for label, names in (("never completed a handshake", [n for n, _ in no_handshake]),
                         ("need credentials", credentials),
                         ("execute or write only", executes),
                         ("no call defined -- catalogue gap", gap),
                         ("call defined, call errored", call_failed)):
        if names:
            print("  {}:".format(label))
            for name in sorted(names):
                print("    {}".format(name))
            print("")

    if failures:
        print("  what the failing calls actually said:")
        for name, call in failures:
            keys = ", ".join(sorted(k for k in call.keys()))
            detail = ""
            for key in ("error", "message", "text", "result", "returned_text"):
                if call.get(key):
                    detail = str(call[key])[:70]
                    break
            print("    {:<28} {}".format(name[:28], detail or "(keys: " + keys + ")"))
        print("")

    print("  ---- paste-ready ----")
    print("")
    print("{} servers attempted, {} started, {} answered a real call. Of the {}"
          .format(total, total - len(no_handshake), len(answered),
                  total - len(answered)))
    print("that did not: {} need credentials or infrastructure I do not have,"
          .format(len(credentials)))
    print("{} declare only tools that execute or write and the read-verb rule"
          .format(len(executes)))
    print("will not call those, {} never completed a handshake, {} had no call"
          .format(len(no_handshake), len(gap)))
    print("defined for them in my catalogue, and {} were asked and errored."
          .format(len(call_failed)))
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
