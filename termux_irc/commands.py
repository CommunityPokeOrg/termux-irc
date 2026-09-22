"""Slash command parsing and dispatch.

Commands operate on a :class:`CommandContext`, which abstracts the pieces
of the running app a command needs (state, current connection, and hooks
to connect/disconnect/quit) so the module is testable without a UI.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .client import IRCClient
from .protocol import is_channel
from .state import STATUS_BUFFER, ClientState
from .termux_api import TermuxAPI, format_battery, format_wifi

ConnectFn = Callable[[str, int, bool], Awaitable[None]]
QuitFn = Callable[[str], Awaitable[None]]


@dataclass
class CommandContext:
    """What a slash command can touch."""

    state: ClientState
    connect: ConnectFn  # (host, port, use_tls) -> connect/reconnect
    disconnect: Callable[[str], Awaitable[None]]
    quit: QuitFn  # (reason) -> quit the app
    default_port: int = 6697
    use_tls: bool = True
    client: IRCClient | None = field(default=None)
    # Optional integrations (set by the app/UI; None-safe everywhere)
    termux: TermuxAPI | None = None
    insert_text: Callable[[str], None] | None = None  # UI input-line hook

    def log(self, text: str, kind: str = "info", to: str | None = None) -> None:
        self.state.log(text, kind=kind, to=to or STATUS_BUFFER)

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.connected

    def require_client(self) -> IRCClient | None:
        if not self.connected:
            self.log("not connected — use /connect <server> first", "error")
            return None
        return self.client


CommandFn = Callable[[CommandContext, list[str], str], Awaitable[None]]


def parse_command(line: str) -> tuple[str, list[str], str]:
    """Split ``/cmd a b rest`` into (command, args, rest-after-first-args).

    ``rest`` is the raw remainder after the command word, so commands that
    take free text can keep spaces intact.
    """
    body = line[1:].strip() if line.startswith("/") else line.strip()
    if not body:
        return "", [], ""
    name, _, rest = body.partition(" ")
    return name.lower(), rest.split() if rest else [], rest


async def _cmd_connect(ctx: CommandContext, args: list[str], rest: str) -> None:
    if not args:
        ctx.log("usage: /connect <server> [port] [tls|plain]", "error")
        return
    host = args[0]
    port = ctx.default_port
    use_tls = ctx.use_tls
    if len(args) >= 2:
        try:
            port = int(args[1])
        except ValueError:
            ctx.log(f"invalid port: {args[1]}", "error")
            return
    if len(args) >= 3:
        opt = args[2].lower()
        if opt in ("tls", "ssl", "ircs", "secure"):
            use_tls = True
        elif opt in ("plain", "notls", "nossl", "irc", "insecure"):
            use_tls = False
        else:
            ctx.log(f"unknown option: {args[2]} (use tls or plain)", "error")
            return
    elif len(args) >= 2:
        # Infer TLS from the conventional port when not stated.
        use_tls = port != 6667
    await ctx.connect(host, port, use_tls)


async def _cmd_disconnect(ctx: CommandContext, args: list[str], rest: str) -> None:
    reason = rest or "leaving"
    await ctx.disconnect(reason)


async def _cmd_join(ctx: CommandContext, args: list[str], rest: str) -> None:
    client = ctx.require_client()
    if client is None:
        return
    if not args:
        ctx.log("usage: /join <#channel> [key]", "error")
        return
    channel = args[0]
    if not is_channel(channel):
        channel = "#" + channel
    key = args[1] if len(args) > 1 else None
    await client.join(channel, key)


async def _cmd_part(ctx: CommandContext, args: list[str], rest: str) -> None:
    client = ctx.require_client()
    if client is None:
        return
    target = args[0] if args else ctx.state.active
    if not is_channel(target):
        ctx.log("usage: /part [#channel] [reason]", "error")
        return
    reason = " ".join(args[1:]) if len(args) > 1 else None
    await client.part(target, reason or None)


async def _cmd_msg(ctx: CommandContext, args: list[str], rest: str) -> None:
    client = ctx.require_client()
    if client is None:
        return
    if len(args) < 2:
        ctx.log("usage: /msg <target> <text>", "error")
        return
    target = args[0]
    text = rest.split(None, 1)[1].strip()
    if not text:
        ctx.log("usage: /msg <target> <text>", "error")
        return
    await client.privmsg(target, text)
    kind = "channel" if is_channel(target) else "query"
    buf = ctx.state.ensure_buffer(target, kind)
    buf.add("msg", text, nick=ctx.state.nick)


async def _cmd_query(ctx: CommandContext, args: list[str], rest: str) -> None:
    if not args:
        ctx.log("usage: /query <nick> [message]", "error")
        return
    nick = args[0]
    buf = ctx.state.ensure_buffer(nick, "query")
    ctx.state.switch(nick)
    text = rest.split(None, 1)[1] if len(rest.split(None, 1)) > 1 else ""
    if text:
        client = ctx.require_client()
        if client is None:
            return
        await client.privmsg(nick, text)
        buf.add("msg", text, nick=ctx.state.nick)


async def _cmd_nick(ctx: CommandContext, args: list[str], rest: str) -> None:
    if not args:
        ctx.log(f"current nick: {ctx.state.nick}", "info")
        return
    client = ctx.require_client()
    if client is None:
        return
    await client.set_nick(args[0])


async def _cmd_names(ctx: CommandContext, args: list[str], rest: str) -> None:
    client = ctx.require_client()
    if client is None:
        return
    channel = args[0] if args else ctx.state.active
    if not is_channel(channel):
        ctx.log("usage: /names [#channel]", "error")
        return
    await client.names(channel)


async def _cmd_whois(ctx: CommandContext, args: list[str], rest: str) -> None:
    client = ctx.require_client()
    if client is None:
        return
    if not args:
        ctx.log("usage: /whois <nick>", "error")
        return
    await client.whois(args[0])


async def _cmd_quit(ctx: CommandContext, args: list[str], rest: str) -> None:
    await ctx.quit(rest or "leaving")


async def _cmd_clear(ctx: CommandContext, args: list[str], rest: str) -> None:
    buf = ctx.state.active_buffer
    buf.lines.clear()
    buf.unread = 0
    buf.scroll = 0


async def _cmd_me(ctx: CommandContext, args: list[str], rest: str) -> None:
    client = ctx.require_client()
    if client is None:
        return
    target = ctx.state.active
    if target == STATUS_BUFFER or not rest.strip():
        ctx.log("usage: /me <action> (in a channel or query)", "error")
        return
    await client.ctcp_action(target, rest)
    ctx.state.active_buffer.add("action", rest, nick=ctx.state.nick)


async def _cmd_topic(ctx: CommandContext, args: list[str], rest: str) -> None:
    client = ctx.require_client()
    if client is None:
        return
    channel = ctx.state.active
    text = rest
    if args and is_channel(args[0]):
        channel = args[0]
        text = rest.split(None, 1)[1] if len(rest.split(None, 1)) > 1 else ""
    if not is_channel(channel):
        ctx.log("usage: /topic [#channel] [new topic]", "error")
        return
    await client.topic(channel, text or None)


async def _cmd_notice(ctx: CommandContext, args: list[str], rest: str) -> None:
    client = ctx.require_client()
    if client is None:
        return
    if len(args) < 2:
        ctx.log("usage: /notice <target> <text>", "error")
        return
    target = args[0]
    text = rest.split(None, 1)[1]
    await client.notice(target, text)
    ctx.state.log(f"-> {target}: {text}", kind="notice")


async def _cmd_raw(ctx: CommandContext, args: list[str], rest: str) -> None:
    client = ctx.require_client()
    if client is None:
        return
    if not rest.strip():
        ctx.log("usage: /raw <irc command line>", "error")
        return
    await client.send(rest)


async def _cmd_window(ctx: CommandContext, args: list[str], rest: str) -> None:
    """Switch buffers: /window <n|name>, /next, /prev, /close."""
    names = list(ctx.state.buffers)
    if not args:
        listing = ", ".join(
            f"{i + 1}:{n}" + ("*" if n == ctx.state.active else "")
            for i, n in enumerate(names)
        )
        ctx.log(f"buffers: {listing}", "info")
        return
    target = args[0]
    if target.isdigit():
        idx = int(target) - 1
        if 0 <= idx < len(names):
            ctx.state.switch(names[idx])
        else:
            ctx.log(f"no buffer #{target}", "error")
        return
    buf = ctx.state.switch(target)
    if buf is None:
        ctx.log(f"no buffer named {target}", "error")


async def _cmd_close(ctx: CommandContext, args: list[str], rest: str) -> None:
    target = args[0] if args else ctx.state.active
    if target == STATUS_BUFFER:
        ctx.log("cannot close the status buffer", "error")
        return
    buf = ctx.state.buffers.get(target)
    if buf and buf.kind == "channel" and buf.joined and ctx.connected:
        client = ctx.require_client()
        if client:
            await client.part(target)
    ctx.state.close_buffer(target)


async def _cmd_help(ctx: CommandContext, args: list[str], rest: str) -> None:
    for line in HELP_TEXT.splitlines():
        ctx.log(line, "info")


async def _cmd_paste(ctx: CommandContext, args: list[str], rest: str) -> None:
    """Paste the Android clipboard into the input line (or show it)."""
    t = ctx.termux
    if t is None or not t.clipboard or not t.active:
        ctx.log("clipboard unavailable (termux-api not installed?)", "error")
        return
    text = await t.clipboard_get()
    if text is None:
        ctx.log("clipboard unavailable (termux-api not installed?)", "error")
        return
    if not text:
        ctx.log("clipboard is empty", "info")
        return
    text = " ".join(text.split())  # flatten to one line
    if ctx.insert_text is not None:
        ctx.insert_text(text)
        ctx.log("pasted clipboard into input", "info")
    else:
        ctx.log(f"clipboard: {text}", "info")


async def _cmd_copy(ctx: CommandContext, args: list[str], rest: str) -> None:
    """Copy text (or the last buffer line) to the Android clipboard."""
    t = ctx.termux
    if t is None or not t.clipboard or not t.active:
        ctx.log("clipboard unavailable (termux-api not installed?)", "error")
        return
    text = rest.strip()
    if not text:
        lines = ctx.state.active_buffer.lines
        if not lines:
            ctx.log("nothing to copy", "error")
            return
        last = lines[-1]
        text = last.text if last.nick is None else f"<{last.nick}> {last.text}"
    if await t.clipboard_set(text):
        ctx.log(f"copied {len(text)} chars to clipboard", "info")
    else:
        ctx.log("clipboard write failed", "error")


async def _cmd_status(ctx: CommandContext, args: list[str], rest: str) -> None:
    """Show client + device status: server, nick, battery, wifi."""
    s = ctx.state
    conn = f"connected to {s.server}" if s.connected else "not connected"
    ctx.log(f"{conn} as {s.nick}; buffers: {len(s.buffers)}", "info")
    t = ctx.termux
    if t is None or not t.system_info or not t.active:
        return
    battery, wifi = await asyncio.gather(t.battery_status(), t.wifi_info())
    if battery:
        ctx.log(format_battery(battery), "info")
    if wifi:
        ctx.log(format_wifi(wifi), "info")
    if not battery and not wifi:
        ctx.log("termux-api system info unavailable", "info")


COMMANDS: dict[str, CommandFn] = {
    "connect": _cmd_connect,
    "server": _cmd_connect,
    "disconnect": _cmd_disconnect,
    "join": _cmd_join,
    "j": _cmd_join,
    "part": _cmd_part,
    "p": _cmd_part,
    "leave": _cmd_part,
    "msg": _cmd_msg,
    "query": _cmd_query,
    "q": _cmd_query,
    "nick": _cmd_nick,
    "names": _cmd_names,
    "whois": _cmd_whois,
    "quit": _cmd_quit,
    "exit": _cmd_quit,
    "clear": _cmd_clear,
    "me": _cmd_me,
    "topic": _cmd_topic,
    "notice": _cmd_notice,
    "raw": _cmd_raw,
    "quote": _cmd_raw,
    "window": _cmd_window,
    "win": _cmd_window,
    "w": _cmd_window,
    "buffer": _cmd_window,
    "close": _cmd_close,
    "paste": _cmd_paste,
    "copy": _cmd_copy,
    "status": _cmd_status,
    "help": _cmd_help,
    "h": _cmd_help,
    "?": _cmd_help,
}

HELP_TEXT = """\
commands:
  /connect <server> [port] [tls|plain]  connect to a server
  /disconnect [reason]                  hang up but stay in the client
  /join <#chan> [key]                   join a channel (/j)
  /part [#chan] [reason]                leave a channel (/p)
  /msg <target> <text>                  send a message to a nick or channel
  /query <nick> [text]                  open a private buffer (/q)
  /nick <newnick>                       change your nick
  /names [#chan]                        list users in a channel
  /whois <nick>                         ask the server about a nick
  /topic [#chan] [text]                 view or set the channel topic
  /me <action>                          send a CTCP action
  /notice <target> <text>               send a NOTICE
  /raw <line>                           send a raw IRC command (/quote)
  /window <n|name>                      switch buffers (/w, /buffer)
  /next /prev                           cycle buffers (or Ctrl-N / Ctrl-P)
  /close [#chan|nick]                   close a buffer (parts if joined)
  /clear                                clear the current buffer
  /paste                                insert clipboard into input (Termux)
  /copy [text]                          copy text/last line to clipboard (Termux)
  /status                               client + battery + wifi status
  /quit [reason]                        disconnect and exit
  /help                                 this help

keys:
  Up/Down        input history        PgUp/PgDn      scroll back
  Ctrl-N/Ctrl-P  next/prev buffer     Alt-1..9       jump to buffer
  Ctrl-A/E       line start/end       Ctrl-U/K/W     clear / kill / del word
  Ctrl-C         quit                 Ctrl-D         quit on empty input
  Tab            complete nicks/commands
"""


async def dispatch_input(ctx: CommandContext, line: str) -> None:
    """Handle one input line: slash command or chat text."""
    line = line.rstrip("\r\n")
    if not line:
        return
    if line.startswith("//"):
        # Escaped leading slash: send literally.
        line = line[1:]
    elif line.startswith("/"):
        name, args, rest = parse_command(line)
        handler = COMMANDS.get(name)
        if handler is None:
            ctx.log(f"unknown command: /{name} (try /help)", "error")
            return
        try:
            await handler(ctx, args, rest)
        except (OSError, RuntimeError, ValueError) as exc:
            ctx.log(f"command failed: {exc}", "error")
        return

    # Plain text goes to the active channel/query.
    target = ctx.state.active
    if target == STATUS_BUFFER or ctx.state.active_buffer.kind == "status":
        ctx.log(
            "this is the status buffer — join a channel or /query someone first",
            "error",
        )
        return
    client = ctx.require_client()
    if client is None:
        return
    try:
        await client.privmsg(target, line)
    except (OSError, RuntimeError, ValueError) as exc:
        ctx.log(f"send failed: {exc}", "error")
        return
    buf = ctx.state.active_buffer
    buf.add("msg", line, nick=ctx.state.nick)


async def _cmd_next(ctx: CommandContext, args: list[str], rest: str) -> None:
    ctx.state.next_buffer(1)


async def _cmd_prev(ctx: CommandContext, args: list[str], rest: str) -> None:
    ctx.state.next_buffer(-1)


COMMANDS["next"] = _cmd_next
COMMANDS["prev"] = _cmd_prev
