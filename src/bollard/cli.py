"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from typing import List, Optional

from . import __version__
from .proxy import MAX_ARG_BYTES_DEFAULT, Proxy
from .record import Recorder
from .report import export_jsonl, format_stats, format_tail

DEFAULT_HOME = Path(os.environ.get("BOLLARD_HOME", Path.home() / ".bollard"))


def cmd_run(args: argparse.Namespace) -> int:
    command: List[str] = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("bollard: no server command given.\n"
              "  usage: bollard run -- <mcp server command>", file=sys.stderr)
        return 2

    recorder = Recorder(Path(args.home), uuid.uuid4().hex[:12],
                        args.label or Path(command[0]).name)
    proxy = Proxy(command, recorder,
                  max_arg_bytes=args.max_arg_bytes,
                  no_args=args.no_args,
                  redact=not args.no_redact,
                  echo=args.echo)
    return proxy.run()


def cmd_stats(args: argparse.Namespace) -> int:
    try:
        sys.stdout.write(format_stats(Path(args.home)))
    except FileNotFoundError:
        print("No data yet. Record some traffic:\n"
              "  bollard run -- <mcp server command>", file=sys.stderr)
        return 1
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    try:
        sys.stdout.write(format_tail(Path(args.home), args.n))
    except FileNotFoundError:
        print("No data yet.", file=sys.stderr)
        return 1
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    try:
        n = export_jsonl(Path(args.home), Path(args.out))
    except FileNotFoundError:
        print("No data yet.", file=sys.stderr)
        return 1
    print(f"wrote {n} records to {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bollard",
        description="Record every tool call an AI agent makes. Block nothing.",
    )
    parser.add_argument("--version", action="version", version=f"bollard {__version__}")
    parser.add_argument("--home", default=str(DEFAULT_HOME),
                        help=f"data directory (default: {DEFAULT_HOME})")
    sub = parser.add_subparsers(dest="command_name", required=True)

    run = sub.add_parser("run", help="wrap and record an MCP server")
    run.add_argument("--label", help="name for this server in the logs")
    run.add_argument("--max-arg-bytes", type=int, default=MAX_ARG_BYTES_DEFAULT,
                     help="cap on stored argument bytes (true size is still recorded)")
    run.add_argument("--no-args", action="store_true",
                     help="record argument shape only, never values")
    run.add_argument("--no-redact", action="store_true",
                     help="store argument values verbatim, including any "
                          "credentials they contain (redaction is on by default)")
    run.add_argument("--echo", action="store_true",
                     help="print one line per call to stderr")
    run.add_argument("command", nargs=argparse.REMAINDER,
                     help="-- followed by the real server command")
    run.set_defaults(func=cmd_run)

    stats = sub.add_parser("stats", help="summarise recorded traffic")
    stats.set_defaults(func=cmd_stats)

    tail = sub.add_parser("tail", help="show the most recent calls")
    tail.add_argument("-n", type=int, default=20)
    tail.set_defaults(func=cmd_tail)

    export = sub.add_parser("export", help="dump all calls as JSONL")
    export.add_argument("out")
    export.set_defaults(func=cmd_export)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


def entrypoint() -> None:
    try:
        sys.exit(main())
    except BrokenPipeError:
        # piping into head/less closes stdout early; not an error
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    entrypoint()
