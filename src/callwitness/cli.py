"""Command line entry point."""

from __future__ import annotations

import argparse
import json
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

DEFAULT_HOME = Path(os.environ.get("BOLLARD_HOME", Path.home() / ".callwitness"))


def cmd_run(args: argparse.Namespace) -> int:
    command: List[str] = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("callwitness: no server command given.\n"
              "  usage: callwitness run -- <mcp server command>", file=sys.stderr)
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
        print("callwitness: --upstream must be an http(s) URL", file=sys.stderr)
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
              "  callwitness run -- <mcp server command>", file=sys.stderr)
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
              "  callwitness run -- <mcp server command>", file=sys.stderr)
        return 1
    return 0


def _cmd_wrap(args: argparse.Namespace, undo: bool) -> int:
    if args.config:
        targets = [("given", Path(args.config))]
        missing = [p for _, p in targets if not p.is_file()]
        if missing:
            print("callwitness: no such file: {}".format(missing[0]), file=sys.stderr)
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
              "  callwitness run -- <mcp server command>", file=sys.stderr)
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


def cmd_contribute(args: argparse.Namespace) -> int:
    """Opt-in contribution. Nothing here runs unless a person typed it.

    Deliberately the only path to `contribute`: no hook in the proxy, no timer,
    no atexit. The proxy promises it cannot delay the stream, and a network call
    it makes on your behalf breaks that promise whether or not it is fast.
    """
    from . import contribute as contrib

    home = Path(args.home)
    config = contrib.load_config(home)
    install = config.get("install")

    if args.forget:
        contrib.save_config(home, {"enabled": False})
        print("Disabled, and the local install id is deleted.")
        if install:
            print("To have data already sent under {} removed, quote that id\n"
                  "at https://github.com/AditiChaudharyy14/callwitness/issues"
                  .format(install))
        return 0

    if args.disable:
        config["enabled"] = False
        contrib.save_config(home, config)
        print("Disabled. Nothing further will be sent.")
        print("The install id is kept so removal of past data is still "
              "possible; use --forget to delete it too.")
        return 0

    if args.enable:
        if config.get("enabled"):
            print("Already on since {}.".format(config.get("since", "?")))
            return 0
        sys.stdout.write(contrib.NOTICE)
        if not args.yes:
            try:
                answer = input("\nEnable? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer not in ("y", "yes"):
                print("Not enabled. Nothing has left this machine.")
                return 1
        install = install or str(uuid.uuid4())
        contrib.save_config(home, {
            "enabled": True, "install": install,
            "since": contrib.datetime.now(contrib.timezone.utc).isoformat(),
        })
        print("\nEnabled. Your install id is {}".format(install))
        print("Stored at {} -- it is the only way to ask for your data back."
              .format(contrib.config_path(home)))
        print("\nNothing is sent until you run:  callwitness contribute --send")
        return 0

    if args.status or not (args.dry_run or args.send):
        if config.get("enabled"):
            print("contribute is ON since {}".format(config.get("since", "?")))
            print("  install id  {}".format(install))
            print("  last sent   {}".format(config.get("last_sent", "never")))
        else:
            print("contribute is OFF. Nothing has been sent.")
        print("\n  callwitness contribute --dry-run   see exactly what would leave")
        print("  callwitness contribute --enable    turn it on")
        return 0

    payload = contrib.build_payload(
        home, install or "00000000-0000-4000-8000-000000000000",
        since=config.get("last_sent_ts"))
    calls = sum(t["calls"] for s in payload["servers"] for t in s["tools"])

    if args.dry_run:
        state = "ON" if config.get("enabled") else "OFF"
        print("contribute is {}. This is what would be sent{}.\n".format(
            state, "" if config.get("enabled") else " if you enabled it"))
        sys.stdout.write(contrib.summarise(payload))
        print()
        print(json.dumps(payload, indent=2, sort_keys=True))
        print("\nNot sent. Nothing has left this machine.")
        if not config.get("enabled"):
            print("To send it:  callwitness contribute --enable"
                  "  then  callwitness contribute --send")
        return 0

    # --send
    if not config.get("enabled"):
        print("contribute is off. Run `callwitness contribute --enable` first.",
              file=sys.stderr)
        return 2
    if not calls:
        print("Nothing new to send since {}.".format(
            config.get("last_sent", "the last send")))
        return 0

    sys.stdout.write(contrib.summarise(payload))
    print()
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not args.yes:
        # Asked every time on purpose. It is how someone notices the day the
        # payload starts containing something new.
        try:
            answer = input("\nSend this? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            print("Not sent. Nothing has left this machine.")
            return 1

    ok, message = contrib.send(payload)
    print(message)
    if not ok:
        return 1
    config["last_sent"] = contrib.datetime.now(contrib.timezone.utc).isoformat()
    config["last_sent_ts"] = payload["window"]["to"]
    contrib.save_config(home, config)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="callwitness",
        description="Record every tool call an AI agent makes. Block nothing.",
    )
    parser.add_argument("--version", action="version", version=f"callwitness {__version__}")
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

    contribute = sub.add_parser(
        "contribute",
        help="opt in to adding the shape of your traffic to a public baseline")
    contribute.add_argument("--status", action="store_true",
                            help="show whether it is on, and the install id")
    contribute.add_argument("--enable", action="store_true",
                            help="turn it on (off by default; nothing is sent "
                                 "until you also run --send)")
    contribute.add_argument("--disable", action="store_true",
                            help="stop sending; keep the install id")
    contribute.add_argument("--forget", action="store_true",
                            help="stop sending and delete the local install id")
    contribute.add_argument("--dry-run", action="store_true",
                            help="print the exact payload and send nothing")
    contribute.add_argument("--send", action="store_true",
                            help="print the payload, confirm, then send it")
    contribute.add_argument("--yes", action="store_true",
                            help="skip the confirmation prompt")
    contribute.set_defaults(func=cmd_contribute)

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
