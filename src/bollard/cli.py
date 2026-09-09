"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from typing import List, Optional

from . import __version__
from .chain import format_report, verify_store
from .http import HttpProxy
from .install import apply as apply_plan
from .install import discover, format_plan, plan
from .proxy import MAX_ARG_BYTES_DEFAULT, Proxy
from .record import Recorder
from .report import export_jsonl, format_stats, format_tail
from .suggest import format_suggestions, format_yaml

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


def cmd_proxy(args: argparse.Namespace) -> int:
    upstream = args.upstream
    if not upstream.startswith(("http://", "https://")):
        print("bollard: --upstream must be an http(s) URL", file=sys.stderr)
        return 2

    from urllib.parse import urlparse
    recorder = Recorder(Path(args.home), uuid.uuid4().hex[:12],
                        args.label or (urlparse(upstream).hostname or "http"))
    server = HttpProxy(upstream, recorder,
                       host=args.host, port=args.port,
                       max_arg_bytes=args.max_arg_bytes,
                       no_args=args.no_args,
                       redact=not args.no_redact,
                       echo=args.echo)
    return server.serve_forever()


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


def cmd_suggest(args: argparse.Namespace) -> int:
    render = format_yaml if args.format == "yaml" else format_suggestions
    try:
        sys.stdout.write(render(Path(args.home), args.since))
    except FileNotFoundError:
        print("No data yet. Record some traffic first:\n"
              "  bollard run -- <mcp server command>", file=sys.stderr)
        return 1
    return 0


def _cmd_wrap(args: argparse.Namespace, undo: bool) -> int:
    if args.config:
        targets = [("given", Path(args.config))]
        missing = [p for _, p in targets if not p.is_file()]
        if missing:
            print("bollard: no such file: {}".format(missing[0]), file=sys.stderr)
            return 2
    else:
        targets = discover()

    reports = [plan(path, undo=undo) for _, path in targets]
    if args.apply:
        for report in reports:
            if report["change"]:
                report["backup"] = apply_plan(report)
    sys.stdout.write(format_plan(reports, undo=undo, applied=args.apply))
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    return _cmd_wrap(args, undo=False)


def cmd_uninstall(args: argparse.Namespace) -> int:
    return _cmd_wrap(args, undo=True)


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        sessions = verify_store(Path(args.home))
    except FileNotFoundError:
        print("No data yet. Record some traffic first:\n"
              "  bollard run -- <mcp server command>", file=sys.stderr)
        return 1
    sys.stdout.write(format_report(sessions))
    # Exit 1 on a broken chain, so this is usable in a cron job or a CI step
    # without anyone having to parse the text.
    return 1 if any(not rep["intact"] for _, _, rep in sessions) else 0


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

    proxy = sub.add_parser(
        "proxy",
        help="record a remote MCP server over Streamable HTTP")
    proxy.add_argument("--upstream", required=True,
                       help="remote MCP endpoint, e.g. https://example.com/mcp")
    proxy.add_argument("--port", type=int, default=8100,
                       help="local port to listen on (default: 8100)")
    proxy.add_argument("--host", default="127.0.0.1",
                       help="local interface to bind (default: 127.0.0.1)")
    proxy.add_argument("--label", help="name for this server in the logs")
    proxy.add_argument("--max-arg-bytes", type=int, default=MAX_ARG_BYTES_DEFAULT,
                       help="cap on stored argument bytes")
    proxy.add_argument("--no-args", action="store_true",
                       help="store argument shape only, never values")
    proxy.add_argument("--no-redact", action="store_true",
                       help="store argument values verbatim, including any "
                            "credentials they contain (redaction is on by default)")
    proxy.add_argument("--echo", action="store_true",
                       help="print each call to stderr as it happens")
    proxy.set_defaults(func=cmd_proxy)

    tail = sub.add_parser("tail", help="show the most recent calls")
    tail.add_argument("-n", type=int, default=20)
    tail.set_defaults(func=cmd_tail)

    suggest = sub.add_parser(
        "suggest",
        help="propose policy rules from traffic already observed")
    suggest.add_argument("--since", type=float, metavar="DAYS",
                         help="only consider calls from the last N days")
    suggest.add_argument("--format", choices=("text", "yaml"), default="text",
                         help="human-readable review (default) or a policy draft")
    suggest.set_defaults(func=cmd_suggest)

    install = sub.add_parser(
        "install",
        help="wrap the MCP servers your client already has (shows the change first)")
    install.add_argument("--apply", action="store_true",
                         help="actually write the change (a backup is made first)")
    install.add_argument("--config", help="a specific config file, if yours is elsewhere")
    install.set_defaults(func=cmd_install)

    uninstall = sub.add_parser(
        "uninstall", help="put the wrapped servers back the way they were")
    uninstall.add_argument("--apply", action="store_true",
                           help="actually write the change (a backup is made first)")
    uninstall.add_argument("--config", help="a specific config file, if yours is elsewhere")
    uninstall.set_defaults(func=cmd_uninstall)

    verify = sub.add_parser(
        "verify",
        help="check that recorded calls have not been altered since they were written")
    verify.set_defaults(func=cmd_verify)

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
