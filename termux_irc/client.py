"""Asyncio IRC connection: registration, read loop, keepalive, sends.

The client is deliberately dumb about IRC semantics beyond what the wire
requires (PING->PONG, nick-in-use, welcome): it parses each line into an
:class:`~termux_irc.protocol.IRCMessage` and hands it to ``on_message``.
Lifecycle changes go to ``on_status``.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from collections.abc import Callable
from typing import Any

from .protocol import (
    IRCMessage,
    encode_line,
    format_line,
    parse_line,
    split_privmsg_text,
)

DEFAULT_PING_AFTER = 120.0  # send a keepalive PING after this much silence
DEFAULT_PING_TIMEOUT = 60.0  # drop the link if no traffic this long after ping


class IRCClient:
    """One asyncio connection to an IRC server."""

    def __init__(
        self,
        host: str,
        port: int,
        nick: str,
        user: str,
        realname: str,
        *,
        password: str | None = None,
        use_tls: bool = True,
        tls_verify: bool = True,
        on_message: Callable[[IRCMessage], Any] | None = None,
        on_status: Callable[[str, str], Any] | None = None,
        ping_after: float = DEFAULT_PING_AFTER,
        ping_timeout: float = DEFAULT_PING_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self.nick = nick
        self.user = user
        self.realname = realname
        self.password = password
        self.use_tls = use_tls
        self.tls_verify = tls_verify
        self.on_message = on_message or (lambda msg: None)
        self.on_status = on_status or (lambda state, detail: None)
        self.ping_after = ping_after
        self.ping_timeout = ping_timeout

        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.registered = False
        self.connected = False
        self.disconnect_reason = ""
        self._closing = False
        self._last_rx = time.monotonic()
        self._watchdog_task: asyncio.Task[None] | None = None

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        """Connect, register, and read until the link drops or we quit.

        Returns when the connection is closed; inspect
        :attr:`disconnect_reason` for why.
        """
        self._status("connecting", f"{self.host}:{self.port}")
        try:
            ssl_ctx = self._make_ssl_context() if self.use_tls else None
            self.reader, self.writer = await asyncio.open_connection(
                self.host, self.port, ssl=ssl_ctx
            )
        except (TimeoutError, OSError, ssl.SSLError) as exc:
            self.disconnect_reason = f"connect failed: {exc}"
            self._status("error", self.disconnect_reason)
            return

        self.connected = True
        self._closing = False
        self._last_rx = time.monotonic()
        self._status("connected", f"{self.host}:{self.port}")
        self._watchdog_task = asyncio.create_task(self._watchdog())
        try:
            await self._register()
            await self._read_loop()
        finally:
            self.connected = False
            if self._watchdog_task:
                self._watchdog_task.cancel()
            await self._close_transport()
            if not self.disconnect_reason:
                self.disconnect_reason = "connection closed"
            self._status("disconnected", self.disconnect_reason)

    async def close(self, reason: str = "leaving") -> None:
        """Politely hang up: send QUIT then close the transport."""
        if self._closing:
            return
        self._closing = True
        self.disconnect_reason = reason
        if self.writer is not None:
            try:
                self._write(format_line("QUIT", reason))
                await self.writer.drain()
            except (OSError, RuntimeError, ValueError):
                pass
        await self._close_transport()

    def _make_ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not self.tls_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _close_transport(self) -> None:
        writer, self.writer = self.writer, None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, RuntimeError, ssl.SSLError):
                pass

    async def _register(self) -> None:
        if self.password:
            self._write(format_line("PASS", self.password))
        self._write(format_line("NICK", self.nick))
        self._write(format_line("USER", self.user, "0", "*", self.realname))
        await self._drain()

    async def _read_loop(self) -> None:
        assert self.reader is not None
        while True:
            try:
                data = await self.reader.readline()
            except (OSError, asyncio.IncompleteReadError, ssl.SSLError) as exc:
                self.disconnect_reason = f"read error: {exc}"
                return
            except ValueError:
                # Line longer than the stream limit; skip it.
                continue
            if not data:
                if not self.disconnect_reason:
                    self.disconnect_reason = "server closed the connection"
                return
            self._last_rx = time.monotonic()
            # Bound memory: only take the first 8 KiB even if the server lies.
            data = data[:8192]
            msg = parse_line(data)
            if not msg.command:
                continue
            self._handle_protocol(msg)
            self.on_message(msg)
            if self._closing:
                return

    def _handle_protocol(self, msg: IRCMessage) -> None:
        """Wire-level replies the client must make on its own."""
        if msg.command == "PING":
            token = msg.trailing or (msg.params[0] if msg.params else "")
            self._write(format_line("PONG", token))
        elif msg.command == "PONG":
            pass
        elif msg.command == "001":
            self.registered = True
            # Track our effective nick (the server may have truncated it).
            if msg.params:
                self.nick = msg.params[0]
        elif msg.command in ("432", "433", "436"):
            # Erroneous / in-use nick before registration: try a variant.
            if not self.registered:
                self.nick = self.nick + "_"
                self._write(format_line("NICK", self.nick))
        elif msg.command == "ERROR" and not self.registered:
            self.disconnect_reason = f"server error: {msg.trailing}"

    async def _watchdog(self) -> None:
        """Keepalive: PING when idle, drop the link when it goes stale."""
        # Tick often enough to notice the ping_after threshold promptly.
        tick = min(5.0, max(0.5, self.ping_after / 2))
        while True:
            await asyncio.sleep(tick)
            idle = time.monotonic() - self._last_rx
            if self._closing:
                return
            if idle > self.ping_after + self.ping_timeout:
                self.disconnect_reason = "ping timeout"
                await self._close_transport()
                return
            if idle > self.ping_after and not self._closing:
                try:
                    self._write(format_line("PING", f"{self.host}-{int(time.time())}"))
                    await self._drain()
                except (OSError, RuntimeError, ValueError):
                    return

    # -- sending ------------------------------------------------------------

    def _write(self, line: str) -> None:
        if self.writer is None:
            raise RuntimeError("not connected")
        self.writer.write(encode_line(line))

    async def _drain(self) -> None:
        if self.writer is not None:
            await self.writer.drain()

    async def send(self, line: str) -> None:
        """Send one raw formatted command line."""
        self._write(line)
        await self._drain()

    async def send_command(self, command: str, *params: str) -> None:
        await self.send(format_line(command, *params))

    async def privmsg(self, target: str, text: str) -> None:
        """Send a PRIVMSG, splitting long text across lines safely."""
        for chunk in split_privmsg_text(text):
            await self.send_command("PRIVMSG", target, chunk)

    async def notice(self, target: str, text: str) -> None:
        for chunk in split_privmsg_text(text):
            await self.send_command("NOTICE", target, chunk)

    async def ctcp_action(self, target: str, text: str) -> None:
        """Send a /me action (CTCP ACTION)."""
        await self.privmsg(target, f"\x01ACTION {text}\x01")

    async def join(self, channel: str, key: str | None = None) -> None:
        params = (channel, key) if key else (channel,)
        await self.send_command("JOIN", *params)

    async def part(self, channel: str, reason: str | None = None) -> None:
        params = (channel, reason) if reason else (channel,)
        await self.send_command("PART", *params)

    async def set_nick(self, nick: str) -> None:
        await self.send_command("NICK", nick)

    async def whois(self, nick: str) -> None:
        await self.send_command("WHOIS", nick, nick)

    async def names(self, channel: str) -> None:
        await self.send_command("NAMES", channel)

    async def topic(self, channel: str, text: str | None = None) -> None:
        if text is None:
            await self.send_command("TOPIC", channel)
        else:
            await self.send_command("TOPIC", channel, text)

    # -- misc ----------------------------------------------------------------

    def _status(self, state: str, detail: str) -> None:
        self.on_status(state, detail)

    def __repr__(self) -> str:
        scheme = "ircs" if self.use_tls else "irc"
        return f"<IRCClient {scheme}://{self.host}:{self.port} as {self.nick}>"


def message_wire_overhead(prefix: str, target: str) -> int:
    """Bytes used by ``:<prefix> PRIVMSG <target> :`` — used for splitting."""
    return len(f":{prefix} PRIVMSG {target} :".encode())
