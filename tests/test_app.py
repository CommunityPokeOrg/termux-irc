"""Tests for server-message → state dispatch (no network)."""

from __future__ import annotations

import pytest

from termux_irc.app import IRCApp, handle_message
from termux_irc.config import Config
from termux_irc.protocol import parse_line
from termux_irc.state import STATUS_BUFFER


@pytest.fixture
def app() -> IRCApp:
    a = IRCApp(Config(nick="me", user="me", realname="me"))
    return a


def feed(app: IRCApp, line: str) -> None:
    handle_message(app, parse_line(line))


def status_lines(app: IRCApp) -> list[str]:
    return [line.text for line in app.state.buffers[STATUS_BUFFER].lines]


def test_welcome_registers(app):
    feed(app, ":srv 001 me :Welcome to the network")
    assert app.state.registered
    assert app.state.nick == "me"
    assert any("Welcome" in t for t in status_lines(app))


def test_motd_logged(app):
    feed(app, ":srv 375 me :- Message of the day -")
    feed(app, ":srv 372 me :- hello motd")
    feed(app, ":srv 376 me :End of MOTD")
    assert any("hello motd" in t for t in status_lines(app))


def test_privmsg_to_channel(app):
    feed(app, ":srv 001 me :Welcome")
    feed(app, ":bob!b@h JOIN #c")
    feed(app, ":bob!b@h PRIVMSG #c :hi all")
    buf = app.state.buffers["#c"]
    line = buf.lines[-1]
    assert line.nick == "bob"
    assert line.text == "hi all"
    assert line.kind == "msg"
    assert buf.unread == 1


def test_highlight_on_mention(app):
    feed(app, ":srv 001 me :Welcome")
    feed(app, ":bob!b@h JOIN #c")
    feed(app, ":bob!b@h PRIVMSG #c :hey me, look")
    buf = app.state.buffers["#c"]
    assert buf.lines[-1].kind == "highlight"
    assert buf.highlight


def test_private_message_opens_query(app):
    feed(app, ":srv 001 me :Welcome")
    feed(app, ":bob!b@h PRIVMSG me :secret")
    buf = app.state.buffers["bob"]
    assert buf.kind == "query"
    assert buf.lines[-1].text == "secret"


def test_ctcp_action(app):
    feed(app, ":srv 001 me :Welcome")
    feed(app, ":bob!b@h JOIN #c")
    feed(app, ":bob!b@h PRIVMSG #c :\x01ACTION dances\x01")
    buf = app.state.buffers["#c"]
    assert buf.lines[-1].kind == "action"
    assert buf.lines[-1].text == "dances"


def test_join_part_tracking(app):
    feed(app, ":srv 001 me :Welcome")
    feed(app, ":me!u@h JOIN #c")
    buf = app.state.buffers["#c"]
    assert buf.joined
    assert app.state.active == "#c"
    feed(app, ":bob!b@h JOIN #c")
    assert "bob" in app.state.channel_users("#c")
    feed(app, ":bob!b@h PART #c :later")
    assert "bob" not in app.state.channel_users("#c")
    feed(app, ":me!u@h PART #c")
    assert not buf.joined


def test_names_reply(app):
    feed(app, ":srv 001 me :Welcome")
    feed(app, ":me!u@h JOIN #c")
    feed(app, ":srv 353 me = #c :@op +voiced plain")
    users = app.state.channel_users("#c")
    assert users == ["op", "voiced", "plain"]
    feed(app, ":srv 366 me #c :End of NAMES")
    assert any("names:" in line.text for line in app.state.buffers["#c"].lines)


def test_nick_change(app):
    feed(app, ":srv 001 me :Welcome")
    feed(app, ":me!u@h JOIN #c")
    feed(app, ":bob!b@h JOIN #c")
    feed(app, ":bob!b@h NICK :robert")
    assert "robert" in app.state.channel_users("#c")
    feed(app, ":me!u@h NICK :myself")
    assert app.state.nick == "myself"


def test_quit_removes_user(app):
    feed(app, ":srv 001 me :Welcome")
    feed(app, ":me!u@h JOIN #c")
    feed(app, ":bob!b@h JOIN #c")
    feed(app, ":bob!b@h QUIT :gone")
    assert "bob" not in app.state.channel_users("#c")
    assert any("quit" in line.text for line in app.state.buffers["#c"].lines)


def test_kick(app):
    feed(app, ":srv 001 me :Welcome")
    feed(app, ":me!u@h JOIN #c")
    feed(app, ":bob!b@h JOIN #c")
    feed(app, ":op!o@h KICK #c bob :spam")
    assert "bob" not in app.state.channel_users("#c")
    feed(app, ":op!o@h KICK #c me :bye")
    assert not app.state.buffers["#c"].joined


def test_topic(app):
    feed(app, ":srv 001 me :Welcome")
    feed(app, ":me!u@h JOIN #c")
    feed(app, ":srv 332 me #c :the topic")
    assert app.state.buffers["#c"].topic == "the topic"
    feed(app, ":bob!b@h TOPIC #c :new topic")
    assert app.state.buffers["#c"].topic == "new topic"


def test_mode_tracking(app):
    feed(app, ":srv 001 me :Welcome")
    feed(app, ":me!u@h JOIN #c")
    feed(app, ":srv 353 me = #c :bob")
    feed(app, ":srv 366 me #c :End")
    feed(app, ":op!o@h MODE #c +o bob")
    assert app.state.buffers["#c"].users["bob"] == "@"
    feed(app, ":op!o@h MODE #c -o bob")
    assert app.state.buffers["#c"].users["bob"] == ""


def test_error_numerics(app):
    feed(app, ":srv 401 me nobody :No such nick")
    assert any("No such nick" in t for t in status_lines(app))


def test_whois_output(app):
    feed(app, ":srv 311 me bob user host * :Bob Realname")
    feed(app, ":srv 318 me bob :End of WHOIS")
    assert any("whois:" in t for t in status_lines(app))


def test_notice_goes_to_status_or_channel(app):
    feed(app, ":srv 001 me :Welcome")
    feed(app, ":me!u@h JOIN #c")
    feed(app, ":srv NOTICE me :*** notice text")
    assert any("notice text" in t for t in status_lines(app))
