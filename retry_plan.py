"""Work out which servers the census missed, and build a catalogue of just those.

    python retry_plan.py              look, change nothing
    python retry_plan.py --write      also write census/data/retry.json

86 attempted, 82 started, 65 answered a real call. The 21 that started and
said nothing, plus the 4 that never started, are the whole of your weakest
number -- and re-running all 86 to reach them costs hours and re-measures the
65 that already worked.

This reads census.jsonl, sorts the records into answered / started-but-silent /
never-started, prints what it found, and writes a catalogue containing only
the ones worth another attempt. It writes nothing unless you pass --write, and
it never touches census.jsonl or servers.json.

The first pass is deliberately dumb about shapes: it prints the keys it finds
before assuming anything, because a wrong guess here produces a catalogue that
silently runs the wrong servers.
"""

import io
import json
import sys
from pathlib import Path

CENSUS = Path("census/data/census.jsonl")
CATALOGUE = Path("census/servers.json")
OUT = Path("census/data/retry.json")


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
    for key in ("package", "server", "name", "id", "slug"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def good_calls(record):
    return [c for c in (record.get("calls") or [])
            if isinstance(c, dict) and not c.get("is_error")]


def started(record):
    """Did the handshake succeed? Declaring tools is the evidence."""
    for key in ("tools", "declared_tools", "tool_count", "declared_bytes"):
        value = record.get(key)
        if isinstance(value, list) and value:
            return True
        if isinstance(value, (int, float)) and value:
            return True
    return False


def main(argv):
    write = "--write" in argv

    if not CENSUS.is_file():
        print("Cannot find {} -- run this from the repo root.".format(CENSUS))
        return 1

    records = load_jsonl(CENSUS)
    if not records:
        print("{} has no readable records.".format(CENSUS))
        return 1

    print("")
    print("  {} records in {}".format(len(records), CENSUS))
    print("  keys on the first record:")
    print("    {}".format(", ".join(sorted(records[0].keys()))))
    print("")

    answered, silent, dead, nameless = [], [], [], 0
    for record in records:
        name = name_of(record)
        if not name:
            nameless += 1
            continue
        if good_calls(record):
            answered.append(name)
        elif started(record):
            silent.append(name)
        else:
            dead.append(name)

    print("  {:>4}  answered a real call".format(len(answered)))
    print("  {:>4}  started, declared tools, returned nothing usable"
          .format(len(silent)))
    print("  {:>4}  never got that far".format(len(dead)))
    if nameless:
        print("  {:>4}  records with no name field I recognised".format(nameless))
    print("")

    retry = sorted(set(silent) | set(dead))
    if not retry:
        print("  Nothing to retry. Every record answered.")
        return 0

    print("  worth another attempt ({}):".format(len(retry)))
    for name in retry:
        mark = "silent" if name in silent else "no start"
        print("    {:<44} {}".format(name[:44], mark))
    print("")

    if not CATALOGUE.is_file():
        print("Cannot find {} -- cannot build a catalogue.".format(CATALOGUE))
        return 1

    catalogue = json.loads(io.open(str(CATALOGUE), encoding="utf-8").read())

    # servers.json is either a list of entries or an object wrapping one.
    if isinstance(catalogue, list):
        entries, wrapper, key = catalogue, None, None
    elif isinstance(catalogue, dict):
        # Not just "the first list" -- servers.json opens with a _comment whose
        # value is a list of prose lines, and picking that silently yields a
        # catalogue of 28 sentences. Take the first list OF ENTRIES instead.
        key = next((k for k, v in catalogue.items()
                    if isinstance(v, list) and not k.startswith("_")
                    and v and isinstance(v[0], dict)), None)
        if key is None:
            print("servers.json has no list of entries in it. Keys: {}"
                  .format(", ".join(sorted(catalogue.keys()))))
            return 1
        entries, wrapper = catalogue[key], catalogue
    else:
        print("servers.json is neither a list nor an object.")
        return 1

    print("  catalogue holds {} entries; keys on the first:".format(len(entries)))
    if entries and isinstance(entries[0], dict):
        print("    {}".format(", ".join(sorted(entries[0].keys()))))
    print("")

    wanted = set(retry)
    picked = [e for e in entries
              if isinstance(e, dict) and name_of(e) in wanted]

    missed = wanted - {name_of(e) for e in picked if isinstance(e, dict)}
    print("  matched {} of {} against the catalogue".format(
        len(picked), len(wanted)))
    if missed:
        print("  no catalogue entry for: {}".format(
            ", ".join(sorted(missed)[:8]) + (" ..." if len(missed) > 8 else "")))
    print("")

    if not write:
        print("  Looks right? Run again with --write to create {}".format(OUT))
        return 0

    if not picked:
        print("  Nothing matched, so nothing was written.")
        return 1

    payload = picked if wrapper is None else dict(wrapper, **{key: picked})
    io.open(str(OUT), "w", encoding="utf-8", newline="").write(
        json.dumps(payload, indent=2) + "\n")

    print("  wrote {} with {} servers".format(OUT, len(picked)))
    print("")
    print("  Then, with a generous handshake timeout, because the first run of")
    print("  an npx server downloads it and that is not the server being slow:")
    print("")
    print("    python census\\census.py --catalogue census\\data\\retry.json "
          "--out census\\data\\census-retry.jsonl --timeout 180")
    print("")
    print("  Add --root and --repo if the first run used them. Leave it going;")
    print("  every call is recorded, which is the other half of the point.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
