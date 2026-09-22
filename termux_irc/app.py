"""Application orchestration: owns state, the connection, and the UI.

``IRCApp`` wires the asyncio client to the buffers and the slash-command
context, runs a reconnect supervisor, and translates server messages into
state updates.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable

from .client import IRCClient
from .commands import CommandContext
from .config import Config
from .protocol import IRCMessage, irc_lower, is_channel
from .state import STATUS_BUFFER, ClientState

RECONNECT_BASE_DELAY = 3.0
RECONNECT_MAX_DELAY = 60.0

# Numerics worth showing in the status buffer verbatim (mildly filtered).
_MOTD_NUMERICS = {"372", "375", "376", "422"}
_INFO_NUMERICS = {
    "001",
    "002",
    "003",
    "004",
    "005",
    "250",
    "251",
    "252",
    "253",
    "254",
    "255",
    "265",
    "266",
}
_WHOIS_NUMERICS = {
    "311",
    "312",
    "313",
    "317",
    "318",
    "319",
    "330",
    "671",
}
_ERROR_NUMERICS = {
    "401",
    "402",
    "403",
    "404",
    "405",
    "421",
    "431",
    "432",
    "433",
    "436",
    "441",
    "442",
    "461",
    "462",
    "464",
    "465",
    "471",
    "473",
    "474",
    "475",
    "482",
}


class IRCApp:
    """The running client application."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = ClientState(
            nick=config.nick, user=config.user, realname=config.realname
        )
        self.state.autojoin = list(config.channels)
        self.client: IRCClient | None = None
        self.on_change: Callable[[], None] = lambda: None
        self.on_quit: Callable[[], None] = lambda: None
        self._quit_event = asyncio.Event()
        self._connect_event = asyncio.Event()
        self._connect_request: tuple[str, int, bool] | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self.ctx = CommandContext(
            state=self.state,
            connect=self.command_connect,
            disconnect=self.command_disconnect,
            quit=self.command_quit,
            default_port=config.resolved_port,
            use_tls=config.tls,
        )

    # -- commands -> app -----------------------------------------------------

    async def command_connect(self, host: str, port: int, use_tls: bool) -> None:
        self.state.log(f"connecting to {host}:{port} (tls={use_tls})…")
        self._connect_request = (host, port, use_tls)
        self.config.server, self.config.port, self.config.tls = host, port, use_tls
        self._connect_event.set()
        if self.client is not None and self.client.connected:
            await self.client.close("changing servers")

    async def command_disconnect(self, reason: str) -> None:
        if self.client is None:
            self.state.log("not connected", "error")
            return
        self._connect_request = None  # stop the supervisor from reconnecting
        await self.client.close(reason)

    async def command_quit(self, reason: str) -> None:
        self.state.quitting = True
        self._quit_event.set()
        self._connect_event.set()
        if self.client is not None:
            await self.client.close(reason)
        self.on_quit()

    # -- lifecycle -----------------------------------------------------------

    async def run(self, ui) -> None:
        """Run the app: supervisor + UI. Returns on quit."""
        self.on_change = ui.invalidate
        self.on_quit = ui.stop
        if self.config.server:
            self._connect_request = (
                self.config.server,
                self.config.resolved_port,
                self.config.tls,
            )
            self._connect_event.set()
        else:
            self.state.log(
                "welcome to termux-irc — /connect <server> to begin, /help for commands"
            )
        self._supervisor = asyncio.create_task(self._supervise())
        try:
            await ui.run()
        finally:
            self.state.quitting = True
            self._quit_event.set()
            self._connect_event.set()
            if self._supervisor:
                self._supervisor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._supervisor
            if self.client is not None:
                await self.client.close("leaving")

    async def _supervise(self) -> None:
        """Maintain the connection; reconnect with backoff when asked."""
        delay = RECONNECT_BASE_DELAY
        while not self.state.quitting:
            await self._connect_event.wait()
            self._connect_event.clear()
            if self.state.quitting or self._connect_request is None:
                continue
            host, port, use_tls = self._connect_request
            client = IRCClient(
                host,
                port,
                self.state.nick,
                self.state.user,
                self.state.realname,
                password=self.config.password,
                use_tls=use_tls,
                tls_verify=self.config.tls_verify,
                on_message=self._on_message,
                on_status=self._on_status,
                ping_after=self.config.ping_after,
                ping_timeout=self.config.ping_timeout,
            )
            self.client = client
            self.ctx.client = client
            await client.run()
            self.state.connected = False
            self.state.registered = False
            self.ctx.client = None
            self.on_change()
            if self.state.quitting:
                break
            if not (self.config.reconnect and self._connect_request):
                continue
            # Reconnect with backoff.
            self.state.log(
                f"disconnected ({client.disconnect_reason}); "
                f"reconnecting in {delay:.0f}s — /disconnect to stop",
                "error",
            )
            self.on_change()
            try:
                await asyncio.wait_for(self._connect_event.wait(), timeout=delay)
                self._connect_event.clear()
                delay = RECONNECT_BASE_DELAY
            except TimeoutError:
                delay = min(delay * 2, RECONNECT_MAX_DELAY)

    # -- client callbacks ------------------------------------------------------

    def _on_status(self, status: str, detail: str) -> None:
        if status == "connected":
            self.state.connected = True
            self.state.server = detail.rsplit(":", 1)[0]
            self.state.log(f"connected to {detail}")
        elif status == "disconnected":
            self.state.connected = False
            for name in self.state.joined_channels():
                self.state.buffers[name].joined = False
            self.state.log(f"disconnected: {detail}", "error")
        elif status == "error":
            self.state.log(detail, "error")
        self.on_change()

    def _on_message(self, msg: IRCMessage) -> None:
        handle_message(self, msg)
        self.on_change()

    def rejoin(self) -> None:
        """Re-join channels after (re)connect: open buffers plus autojoin."""
        if self.client is None or not self.client.connected:
            return
        channels = {n for n, b in self.state.buffers.items() if b.kind == "channel"} | set(
            self.state.autojoin
        )
        for chan in sorted(channels):
            asyncio.get_running_loop().create_task(self.client.join(chan))


# -- server message dispatch -------------------------------------------------


def _me(state: ClientState) -> str:
    return state.nick


def handle_message(app: IRCApp, msg: IRCMessage) -> None:
    """Update state buffers from one server message."""
    state = app.state
    cmd = msg.command
    nick = msg.nick or msg.prefix or "?"

    if cmd in _MOTD_NUMERICS or cmd in _INFO_NUMERICS:
        text = msg.trailing or " ".join(msg.params[1:])
        state.log(text, to=STATUS_BUFFER)
        if cmd == "001" and msg.params:
            state.nick = msg.params[0]
            state.registered = True
            app.rejoin()
        return
    if cmd in _WHOIS_NUMERICS:
        state.log("whois: " + " ".join(msg.params[1:]), to=STATUS_BUFFER)
        return
    if cmd in _ERROR_NUMERICS:
        text = msg.trailing or " ".join(msg.params[1:])
        target = msg.params[1] if len(msg.params) > 1 else ""
        detail = f"{target}: {text}".strip(": ") if target else text
        state.log(detail, "error", to=STATUS_BUFFER)
        if cmd == "433":
            state.log(f"nick in use; trying {state.nick}_", "error")
        return
    if cmd == "353":  # NAMES reply: <me> <sym> <chan> :[prefix]nick ...
        channel = msg.params[2] if len(msg.params) > 2 else ""
        for entry in msg.trailing.split():
            prefix = ""
            while entry and entry[0] in "@+%&~":
                prefix += entry[0]
                entry = entry[1:]
            if entry:
                state.add_user(channel, entry, prefix)
        return
    if cmd == "366":  # end of NAMES
        channel = msg.params[1] if len(msg.params) > 1 else state.active
        users = state.channel_users(channel)
        buf = state.buffers.get(channel)
        if buf:
            buf.add("info", f"names: {', '.join(users)}")
        return
    if cmd == "332":  # topic
        channel = msg.params[1] if len(msg.params) > 1 else ""
        buf = state.buffers.get(channel)
        if buf:
            buf.topic = msg.trailing
            buf.add("info", f"topic: {msg.trailing}")
        return
    if cmd == "331":
        channel = msg.params[1] if len(msg.params) > 1 else ""
        buf = state.buffers.get(channel)
        if buf:
            buf.add("info", "no topic set")
        return
    if cmd == "324":  # channel modes
        channel = msg.params[1] if len(msg.params) > 1 else ""
        buf = state.buffers.get(channel)
        if buf:
            buf.add("info", f"modes: {' '.join(msg.params[2:])}")
        return

    if cmd in ("PRIVMSG", "NOTICE"):
        target = msg.params[0] if msg.params else ""
        text = msg.trailing
        from_me = irc_lower(nick) == irc_lower(state.nick)
        to_me = irc_lower(target) == irc_lower(state.nick)
        # CTCP ACTION (/me)
        if text.startswith("\x01ACTION ") and text.endswith("\x01"):
            kind = "action"
            text = text[len("\x01ACTION ") : -1]
        elif text.startswith("\x01"):
            kind = "notice"
            text = f"CTCP {text.strip(chr(1))}"
        else:
            kind = "msg" if cmd == "PRIVMSG" else "notice"
        # Highlight when someone mentions us in a channel.
        if (
            kind in ("msg", "action")
            and not from_me
            and not to_me
            and state.nick
            and re.search(rf"\b{re.escape(state.nick)}\b", text, re.IGNORECASE)
        ):
            kind = "highlight"
        if from_me and not to_me:
            # Echo of our own message (servers that echo) — place in target buf.
            dest = target
        elif to_me:
            dest = nick
            buf = state.ensure_buffer(nick, "query")
            if kind == "notice":
                dest = STATUS_BUFFER
        else:
            dest = target
        buf = state.ensure_buffer(dest)
        buf.add(kind, text, nick=nick)
        if dest != state.active:
            buf.unread += 1
            if kind == "highlight":
                buf.highlight = True
        return

    if cmd == "JOIN":
        channel = msg.trailing or (msg.params[0] if msg.params else "")
        buf = state.ensure_buffer(channel, "channel")
        if irc_lower(nick) == irc_lower(state.nick):
            buf.joined = True
            buf.add("info", f"you joined {channel}")
            state.switch(channel)
        else:
            state.add_user(channel, nick)
            buf.add("info", f"{nick} joined")
        return
    if cmd == "PART":
        channel = msg.params[0] if msg.params else ""
        buf = state.ensure_buffer(channel, "channel")
        if irc_lower(nick) == irc_lower(state.nick):
            buf.joined = False
            buf.users.clear()
            buf.user_display.clear()
            buf.add("info", f"you left {channel}")
        else:
            state.remove_user(channel, nick)
            reason = f" ({msg.trailing})" if len(msg.params) > 1 else ""
            buf.add("info", f"{nick} left{reason}")
        return
    if cmd == "QUIT":
        reason = msg.trailing
        for name in state.buffers:
            buf = state.buffers[name]
            if irc_lower(nick) in buf.users:
                state.remove_user(name, nick)
                buf.add("info", f"{nick} quit ({reason})")
        return
    if cmd == "NICK":
        new = msg.trailing or (msg.params[0] if msg.params else "")
        if irc_lower(nick) == irc_lower(state.nick):
            state.nick = new
            state.log(f"you are now known as {new}", to=STATUS_BUFFER)
        for name in state.rename_user(nick, new):
            state.buffers[name].add("info", f"{nick} is now {new}")
        return
    if cmd == "KICK":
        channel = msg.params[0] if msg.params else ""
        kicked = msg.params[1] if len(msg.params) > 1 else ""
        buf = state.ensure_buffer(channel, "channel")
        if irc_lower(kicked) == irc_lower(state.nick):
            buf.joined = False
            buf.users.clear()
            buf.user_display.clear()
            buf.add("error", f"you were kicked by {nick} ({msg.trailing})")
        else:
            state.remove_user(channel, kicked)
            buf.add("info", f"{nick} kicked {kicked} ({msg.trailing})")
        return
    if cmd == "MODE":
        if len(msg.params) >= 2 and is_channel(msg.params[0]):
            channel = msg.params[0]
            _apply_mode(state, channel, msg.params[1], msg.params[2:])
            buf = state.buffers.get(channel)
            if buf:
                buf.add("info", f"{nick} sets mode {' '.join(msg.params[1:])}")
        else:
            state.log(f"{nick} sets mode {' '.join(msg.params)}", to=STATUS_BUFFER)
        return
    if cmd == "TOPIC":
        channel = msg.params[0] if msg.params else ""
        buf = state.buffers.get(channel)
        if buf:
            buf.topic = msg.trailing
            buf.add("info", f"{nick} set topic: {msg.trailing}")
        return
    if cmd == "INVITE":
        state.log(f"{nick} invited you to {msg.trailing}", to=STATUS_BUFFER)
        return
    if cmd == "ERROR":
        state.log(f"server error: {msg.trailing}", "error", to=STATUS_BUFFER)
        return
    if cmd in ("PONG",):
        state.log(f"pong: {msg.trailing}", to=STATUS_BUFFER)
        return

    # Everything else: show compactly in status.
    detail = " ".join(msg.params) or msg.trailing
    state.log(f"[{cmd}] {detail}", "raw", to=STATUS_BUFFER)


def _apply_mode(state: ClientState, channel: str, modes: str, args: list[str]) -> None:
    """Track +o/+v membership prefixes from MODE lines."""
    buf = state.buffers.get(channel)
    if buf is None:
        return
    adding = True
    argi = 0
    for ch in modes:
        if ch == "+":
            adding = True
        elif ch == "-":
            adding = False
        elif ch in "ov" and argi < len(args):
            nick = args[argi]
            argi += 1
            lnick = irc_lower(nick)
            if lnick in buf.users:
                flag = "@" if ch == "o" else "+"
                cur = buf.users[lnick]
                buf.users[lnick] = (
                    (cur + flag)
                    if adding and flag not in cur
                    else (cur.replace(flag, "") if not adding else cur)
                )
        elif ch in "beIqkl" and argi < len(args):
            argi += 1  # modes that take a parameter we don't track
