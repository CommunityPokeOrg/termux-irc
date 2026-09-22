"""Tests for buffer/channel state tracking."""

from __future__ import annotations

from termux_irc.state import STATUS_BUFFER, ClientState


def test_status_buffer_exists():
    s = ClientState(nick="me")
    assert STATUS_BUFFER in s.buffers
    assert s.active == STATUS_BUFFER
    assert s.active_buffer.kind == "status"


def test_ensure_buffer_kinds():
    s = ClientState(nick="me")
    chan = s.ensure_buffer("#c")
    assert chan.kind == "channel"
    q = s.ensure_buffer("someone")
    assert q.kind == "query"
    # Second call returns the same buffer.
    assert s.ensure_buffer("#c") is chan
    # Status stays first.
    assert list(s.buffers)[0] == STATUS_BUFFER


def test_switch_clears_unread():
    s = ClientState(nick="me")
    buf = s.ensure_buffer("#c")
    buf.add("msg", "hi", nick="a")
    buf.unread = 5
    buf.highlight = True
    s.switch("#c")
    assert buf.unread == 0
    assert not buf.highlight
    assert s.active == "#c"


def test_next_buffer_cycles():
    s = ClientState(nick="me")
    s.ensure_buffer("#a")
    s.ensure_buffer("#b")
    s.switch("#a")
    s.next_buffer(1)
    assert s.active == "#b"
    s.next_buffer(1)
    assert s.active == STATUS_BUFFER
    s.next_buffer(-1)
    assert s.active == "#b"


def test_close_buffer():
    s = ClientState(nick="me")
    s.ensure_buffer("#a")
    s.switch("#a")
    s.close_buffer("#a")
    assert "#a" not in s.buffers
    assert s.active == STATUS_BUFFER
    # Status buffer can't be closed.
    s.close_buffer(STATUS_BUFFER)
    assert STATUS_BUFFER in s.buffers


def test_log_to_inactive_marks_unread():
    s = ClientState(nick="me")
    s.ensure_buffer("#x")
    s.switch("#x")
    s.log("hello", kind="msg", to="#y")
    assert s.buffers["#y"].unread == 1
    s.log("m2", kind="msg", to="#y")
    assert s.buffers["#y"].unread == 2


def test_membership_tracking():
    s = ClientState(nick="me")
    s.add_user("#c", "alice", "@")
    s.add_user("#c", "bob", "+")
    s.add_user("#c", "carol")
    users = s.channel_users("#c")
    assert users == ["alice", "bob", "carol"]  # ops, voiced, then alpha
    s.remove_user("#c", "bob")
    assert "bob" not in s.channel_users("#c")


def test_membership_casemap():
    s = ClientState(nick="me")
    s.add_user("#c", "Nick{Name}")
    s.remove_user("#c", "nick{name}")
    assert s.channel_users("#c") == []


def test_rename_user():
    s = ClientState(nick="me")
    s.add_user("#c", "old", "@")
    affected = s.rename_user("old", "new")
    assert affected == ["#c"]
    assert s.channel_users("#c") == ["new"]
    buf = s.buffers["#c"]
    assert buf.users["new"] == "@"


def test_rename_follows_query_buffer():
    s = ClientState(nick="me")
    s.ensure_buffer("oldnick", "query")
    s.switch("oldnick")
    s.rename_user("oldnick", "newnick")
    assert "newnick" in s.buffers
    assert "oldnick" not in s.buffers
    assert s.active == "newnick"


def test_joined_channels():
    s = ClientState(nick="me")
    a = s.ensure_buffer("#a")
    b = s.ensure_buffer("#b")
    a.joined = True
    assert s.joined_channels() == ["#a"]
    b.joined = True
    assert set(s.joined_channels()) == {"#a", "#b"}
