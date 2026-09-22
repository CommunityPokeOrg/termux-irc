"""Client tests against a real asyncio loopback IRC server."""

from __future__ import annotations

import asyncio

from termux_irc.client import IRCClient
from termux_irc.protocol import parse_line


class FakeServer:
    """A minimal scripted IRC server on localhost."""

    def __init__(self):
        self.lines: list[str] = []
        self.writer: asyncio.StreamWriter | None = None
        self.reader: asyncio.StreamReader | None = None
        self.server: asyncio.base_events.Server | None = None
        self.connected = asyncio.Event()

    async def __aenter__(self) -> FakeServer:
        self.server = await asyncio.start_server(self._conn, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc):
        if self.writer:
            self.writer.close()
        self.server.close()
        await self.server.wait_closed()

    @property
    def port(self) -> int:
        return self.server.sockets[0].getsockname()[1]

    async def _conn(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.connected.set()

    async def recv(self, timeout: float = 5.0) -> str:
        await asyncio.wait_for(self.connected.wait(), timeout)
        assert self.reader is not None
        data = await asyncio.wait_for(self.reader.readline(), timeout)
        line = data.decode("utf-8").rstrip("\r\n")
        self.lines.append(line)
        return line

    async def expect(self, command: str, timeout: float = 5.0) -> str:
        """Read lines until one starts with ``command``."""
        await asyncio.wait_for(self.connected.wait(), timeout)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            assert remaining > 0, f"timed out waiting for {command}; got {self.lines}"
            line = await self.recv(timeout=remaining)
            msg = parse_line(line)
            if msg.command == command:
                return line

    async def send(self, line: str) -> None:
        assert self.writer is not None
        self.writer.write(line.encode() + b"\r\n")
        await self.writer.drain()

    def close_conn(self) -> None:
        if self.writer:
            self.writer.close()


def make_client(port: int, **kw) -> IRCClient:
    messages = kw.pop("messages", [])
    statuses = kw.pop("statuses", [])
    client = IRCClient(
        "127.0.0.1",
        port,
        nick="tux",
        user="tux",
        realname="test user",
        use_tls=False,
        on_message=messages.append,
        on_status=lambda s, d: statuses.append((s, d)),
        ping_after=kw.pop("ping_after", 120),
        ping_timeout=kw.pop("ping_timeout", 60),
        **kw,
    )
    client._test_messages = messages  # type: ignore[attr-defined]
    client._test_statuses = statuses  # type: ignore[attr-defined]
    return client


def test_registration_and_ping():
    async def scenario():
        messages, statuses = [], []
        async with FakeServer() as srv:
            client = make_client(srv.port, messages=messages, statuses=statuses)
            task = asyncio.create_task(client.run())
            assert (await srv.expect("NICK")).startswith("NICK tux")
            assert "USER tux 0 * :test user" in await srv.expect("USER")
            await srv.send(":srv 001 tux :Welcome")
            await asyncio.sleep(0.05)
            assert client.registered
            assert statuses[0][0] == "connecting"
            assert ("connected", f"127.0.0.1:{srv.port}") in statuses

            await srv.send(":srv PING :token42")
            assert "PONG token42" in await srv.expect("PONG")

            srv.close_conn()
            await asyncio.wait_for(task, 5)
            assert not client.connected
            assert statuses[-1][0] == "disconnected"

    asyncio.run(scenario())


def test_password_and_registration_order():
    async def scenario():
        async with FakeServer() as srv:
            client = make_client(srv.port, password="hunter2")
            task = asyncio.create_task(client.run())
            assert await srv.expect("PASS") == "PASS hunter2"
            await srv.expect("NICK")
            await srv.expect("USER")
            srv.close_conn()
            await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_nick_in_use_retries_with_underscore():
    async def scenario():
        async with FakeServer() as srv:
            client = make_client(srv.port)
            task = asyncio.create_task(client.run())
            await srv.expect("NICK")
            await srv.expect("USER")
            await srv.send(":srv 433 * tux :Nickname is already in use")
            await asyncio.sleep(0.05)
            assert client.nick == "tux_"
            assert (await srv.expect("NICK")) == "NICK tux_"
            await srv.send(":srv 001 tux_ :Welcome")
            await asyncio.sleep(0.05)
            assert client.registered
            srv.close_conn()
            await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_privmsg_and_join_sends():
    async def scenario():
        async with FakeServer() as srv:
            client = make_client(srv.port)
            task = asyncio.create_task(client.run())
            await srv.expect("USER")
            await srv.send(":srv 001 tux :Welcome")
            await asyncio.sleep(0.05)
            await client.join("#chan")
            assert "JOIN #chan" in await srv.expect("JOIN")
            await client.privmsg("#chan", "hi there")
            assert "PRIVMSG #chan :hi there" in await srv.expect("PRIVMSG")
            await client.ctcp_action("#chan", "waves")
            line = await srv.expect("PRIVMSG")
            assert "\x01ACTION waves\x01" in line
            srv.close_conn()
            await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_long_message_split():
    async def scenario():
        async with FakeServer() as srv:
            client = make_client(srv.port)
            task = asyncio.create_task(client.run())
            await srv.expect("USER")
            await srv.send(":srv 001 tux :Welcome")
            await asyncio.sleep(0.05)
            await client.privmsg("#c", "x" * 900)
            first = await srv.expect("PRIVMSG")
            second = await srv.recv()
            assert len(first.encode()) <= 512
            assert len(second.encode()) <= 512
            srv.close_conn()
            await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_messages_dispatched():
    async def scenario():
        messages = []
        async with FakeServer() as srv:
            client = make_client(srv.port, messages=messages)
            task = asyncio.create_task(client.run())
            await srv.expect("USER")
            await srv.send(":srv 001 tux :Welcome")
            await srv.send(":bob!b@h PRIVMSG tux :hello")
            await asyncio.sleep(0.05)
            privmsgs = [m for m in messages if m.command == "PRIVMSG"]
            assert privmsgs and privmsgs[0].trailing == "hello"
            assert privmsgs[0].nick == "bob"
            srv.close_conn()
            await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_graceful_quit():
    async def scenario():
        async with FakeServer() as srv:
            client = make_client(srv.port)
            task = asyncio.create_task(client.run())
            await srv.expect("USER")
            await srv.send(":srv 001 tux :Welcome")
            await asyncio.sleep(0.05)
            await client.close("bye bye")
            assert "QUIT :bye bye" in await srv.expect("QUIT")
            await asyncio.wait_for(task, 5)
            assert client.disconnect_reason == "bye bye"

    asyncio.run(scenario())


def test_connect_failure():
    async def scenario():
        statuses = []
        # Nothing listens on this port.
        client = make_client(1, statuses=statuses)
        await client.run()
        assert statuses[-1][0] == "error"
        assert not client.connected

    asyncio.run(scenario())


def test_ping_watchdog():
    async def scenario():
        async with FakeServer() as srv:
            client = make_client(srv.port, ping_after=0.2, ping_timeout=10)
            # Shrink watchdog sleep for the test.
            task = asyncio.create_task(client.run())
            await srv.expect("USER")
            await srv.send(":srv 001 tux :Welcome")
            # Watchdog sleeps 5s between checks; patch the loop cadence by
            # waiting for the keepalive PING (idle > ping_after).
            line = await srv.expect("PING", timeout=8)
            assert line.startswith("PING")
            srv.close_conn()
            await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
