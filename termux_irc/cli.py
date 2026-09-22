"""Command-line entry point: arg parsing, config merge, asyncio boot."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from . import __version__
from .app import IRCApp
from .commands import dispatch_input
from .config import DEFAULT_CONFIG_PATH, Config, build_config
from .ui import ChatUI


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="termux-irc",
        description="A clean asyncio IRC client optimized for Termux on Android.",
        epilog=(
            f"config file: {DEFAULT_CONFIG_PATH} "
            "(see config.example.toml); env vars: TERMUX_IRC_*"
        ),
    )
    p.add_argument("server", nargs="?", help="IRC server host to connect to")
    p.add_argument("--port", type=int, help="server port (default 6697 tls / 6667 plain)")
    p.add_argument(
        "--tls", dest="tls", action="store_true", default=None, help="use TLS (default)"
    )
    p.add_argument(
        "--no-tls", dest="tls", action="store_false", help="plain-text connection"
    )
    p.add_argument(
        "--insecure",
        dest="tls_verify",
        action="store_false",
        default=None,
        help="skip TLS certificate verification",
    )
    p.add_argument("--nick", help="nickname")
    p.add_argument("--user", help="username sent in USER")
    p.add_argument("--realname", help="real name sent in USER")
    p.add_argument(
        "--password", help="server password (prefer TERMUX_IRC_PASSWORD env var)"
    )
    p.add_argument(
        "--channel",
        dest="channels",
        action="append",
        default=None,
        metavar="#CHAN",
        help="join channel on connect (repeatable)",
    )
    p.add_argument(
        "--no-reconnect",
        dest="reconnect",
        action="store_false",
        default=None,
        help="do not auto-reconnect on disconnect",
    )
    p.add_argument("--config", type=Path, default=None, help="path to a TOML config file")
    p.add_argument("--version", action="store_true", help="print version and exit")
    return p


def _cli_overrides(args: argparse.Namespace) -> dict[str, object]:
    out: dict[str, object] = {}
    if args.server:
        out["server"] = args.server
    for key in (
        "port",
        "tls",
        "tls_verify",
        "nick",
        "user",
        "realname",
        "password",
        "channels",
        "reconnect",
    ):
        value = getattr(args, key, None)
        if value is not None:
            out[key] = value
    return out


async def _amain(config: Config) -> int:
    app = IRCApp(config)
    ui = ChatUI(
        app.state,
        submit=lambda line: dispatch_input(app.ctx, line),
        request_quit=lambda: asyncio.ensure_future(app.command_quit("leaving")),
    )
    try:
        await app.run(ui)
    except KeyboardInterrupt:
        await app.command_quit("leaving")
    finally:
        ui.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"termux-irc {__version__}")
        return 0
    config = build_config(
        cli_overrides=_cli_overrides(args),
        config_path=args.config,
    )
    if not os.environ.get("TERM"):
        # Termux always sets TERM; a missing one usually means a broken env.
        os.environ["TERM"] = "xterm-256color"
    try:
        return asyncio.run(_amain(config))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
