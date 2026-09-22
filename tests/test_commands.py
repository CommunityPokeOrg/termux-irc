"""Tests for slash-command parsing and dispatch (no network)."""

from __future__ import annotations

import asyncio

from termux_irc.commands import COMMANDS, CommandContext, dispatch_input, parse_command
from termux_irc.state import STATUS_BUFFER, ClientState


class FakeClient:
    """Records commands instead of doing I/O."""

    def __init__(self, connected: bool = True):
        self.connected = connected
        self.sent: list[tuple] = []

    async def privmsg(self, target, text):
        self.sent.append(("privmsg", target, text))

    async def notice(self, target, text):
        self.sent.append(("notice", target, text))

    async def ctcp_action(self, target, text):
        self.sent.append(("action", target, text))

    async def join(self, channel, key=None):
        self.sent.append(("join", channel, key))

    async def part(self, channel, reason=None):
        self.sent.append(("part", channel, reason))

    async def set_nick(self, nick):
        self.sent.append(("nick", nick))

    async def whois(self, nick):
        self.sent.append(("whois", nick))

    async def names(self, channel):
        self.sent.append(("names", channel))

    async def topic(self, channel, text=None):
        self.sent.append(("topic", channel, text))

    async def send(self, line):
        self.sent.append(("raw", line))


def make_ctx(client: FakeClient | None = None) -> CommandContext:
    calls: dict[str, list] = {"connect": [], "disconnect": [], "quit": []}

    async def connect(host, port, tls):
        calls["connect"].append((host, port, tls))

    async def disconnect(reason):
        calls["disconnect"].append(reason)
        if client is not None:
            client.connected = False

    async def quit_(reason):
        calls["quit"].append(reason)

    ctx = CommandContext(
        state=ClientState(nick="me"),
        connect=connect,
        disconnect=disconnect,
        quit=quit_,
        client=client,
    )
    ctx.calls = calls  # type: ignore[attr-defined]
    return ctx


def run(coro):
    return asyncio.run(coro)


class TestParseCommand:
    def test_basic(self):
        name, args, rest = parse_command("/join #chan key")
        assert name == "join"
        assert args == ["#chan", "key"]
        assert rest == "#chan key"

    def test_no_args(self):
        name, args, rest = parse_command("/quit")
        assert name == "quit"
        assert args == []

    def test_case(self):
        name, _, _ = parse_command("/JOIN #c")
        assert name == "join"


class TestDispatch:
    def test_plain_text_to_channel(self):
        client = FakeClient()
        ctx = make_ctx(client)
        ctx.state.ensure_buffer("#c")
        ctx.state.switch("#c")
        run(dispatch_input(ctx, "hello world"))
        assert client.sent == [("privmsg", "#c", "hello world")]
        buf = ctx.state.buffers["#c"]
        assert buf.lines[-1].text == "hello world"
        assert buf.lines[-1].nick == "me"

    def test_text_in_status_buffer_errors(self):
        ctx = make_ctx(FakeClient())
        run(dispatch_input(ctx, "hello"))
        assert ctx.state.buffers[STATUS_BUFFER].lines[-1].kind == "error"

    def test_text_when_disconnected_errors(self):
        ctx = make_ctx(FakeClient(connected=False))
        ctx.state.ensure_buffer("#c")
        ctx.state.switch("#c")
        run(dispatch_input(ctx, "hi"))
        assert ctx.state.buffers[STATUS_BUFFER].lines[-1].kind == "error"

    def test_unknown_command(self):
        ctx = make_ctx(FakeClient())
        run(dispatch_input(ctx, "/frobnicate"))
        line = ctx.state.buffers[STATUS_BUFFER].lines[-1]
        assert "unknown command" in line.text

    def test_join(self):
        client = FakeClient()
        ctx = make_ctx(client)
        run(dispatch_input(ctx, "/join #chan"))
        assert client.sent == [("join", "#chan", None)]

    def test_join_adds_hash(self):
        client = FakeClient()
        ctx = make_ctx(client)
        run(dispatch_input(ctx, "/j chan"))
        assert client.sent == [("join", "#chan", None)]

    def test_part_active_channel(self):
        client = FakeClient()
        ctx = make_ctx(client)
        ctx.state.ensure_buffer("#c")
        ctx.state.switch("#c")
        run(dispatch_input(ctx, "/part"))
        assert client.sent == [("part", "#c", None)]

    def test_msg(self):
        client = FakeClient()
        ctx = make_ctx(client)
        run(dispatch_input(ctx, "/msg bob hi there"))
        assert client.sent == [("privmsg", "bob", "hi there")]
        assert ctx.state.buffers["bob"].lines[-1].text == "hi there"

    def test_msg_requires_text(self):
        ctx = make_ctx(FakeClient())
        run(dispatch_input(ctx, "/msg bob"))
        assert ctx.state.buffers[STATUS_BUFFER].lines[-1].kind == "error"

    def test_query_opens_buffer(self):
        client = FakeClient()
        ctx = make_ctx(client)
        run(dispatch_input(ctx, "/query alice hello"))
        assert ctx.state.active == "alice"
        assert client.sent == [("privmsg", "alice", "hello")]

    def test_nick(self):
        client = FakeClient()
        ctx = make_ctx(client)
        run(dispatch_input(ctx, "/nick newname"))
        assert client.sent == [("nick", "newname")]

    def test_me_action(self):
        client = FakeClient()
        ctx = make_ctx(client)
        ctx.state.ensure_buffer("#c")
        ctx.state.switch("#c")
        run(dispatch_input(ctx, "/me waves"))
        assert client.sent == [("action", "#c", "waves")]
        assert ctx.state.buffers["#c"].lines[-1].kind == "action"

    def test_raw(self):
        client = FakeClient()
        ctx = make_ctx(client)
        run(dispatch_input(ctx, "/raw MODE #c +o bob"))
        assert client.sent == [("raw", "MODE #c +o bob")]

    def test_quit_calls_hook(self):
        ctx = make_ctx(FakeClient())
        run(dispatch_input(ctx, "/quit bye now"))
        assert ctx.calls["quit"] == ["bye now"]

    def test_connect_parses(self):
        ctx = make_ctx()
        run(dispatch_input(ctx, "/connect irc.example.com 6697 tls"))
        assert ctx.calls["connect"] == [("irc.example.com", 6697, True)]

    def test_connect_plain(self):
        ctx = make_ctx()
        run(dispatch_input(ctx, "/connect irc.example.com 6667"))
        assert ctx.calls["connect"] == [("irc.example.com", 6667, False)]

    def test_connect_default(self):
        ctx = make_ctx()
        run(dispatch_input(ctx, "/connect irc.example.com"))
        assert ctx.calls["connect"] == [("irc.example.com", 6697, True)]

    def test_clear(self):
        ctx = make_ctx()
        buf = ctx.state.buffers[STATUS_BUFFER]
        buf.add("info", "old")
        run(dispatch_input(ctx, "/clear"))
        assert len(buf.lines) == 0

    def test_window_switch(self):
        ctx = make_ctx()
        ctx.state.ensure_buffer("#a")
        run(dispatch_input(ctx, "/window #a"))
        assert ctx.state.active == "#a"
        run(dispatch_input(ctx, "/w 1"))
        assert ctx.state.active == STATUS_BUFFER

    def test_next_prev(self):
        ctx = make_ctx()
        ctx.state.ensure_buffer("#a")
        run(dispatch_input(ctx, "/next"))
        assert ctx.state.active == "#a"
        run(dispatch_input(ctx, "/prev"))
        assert ctx.state.active == STATUS_BUFFER

    def test_double_slash_literal(self):
        client = FakeClient()
        ctx = make_ctx(client)
        ctx.state.ensure_buffer("#c")
        ctx.state.switch("#c")
        run(dispatch_input(ctx, "//literal slash"))
        assert client.sent == [("privmsg", "#c", "/literal slash")]

    def test_close_parts_channel(self):
        client = FakeClient()
        ctx = make_ctx(client)
        buf = ctx.state.ensure_buffer("#c")
        buf.joined = True
        ctx.state.switch("#c")
        run(dispatch_input(ctx, "/close"))
        assert "#c" not in ctx.state.buffers
        assert ("part", "#c", None) in client.sent

    def test_all_commands_have_handlers(self):
        for name in (
            "/connect",
            "/join",
            "/part",
            "/msg",
            "/query",
            "/nick",
            "/names",
            "/whois",
            "/quit",
            "/help",
            "/clear",
        ):
            assert name[1:] in COMMANDS
