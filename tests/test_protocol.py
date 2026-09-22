"""Tests for IRC wire-format parsing and formatting."""

from __future__ import annotations

import pytest

from termux_irc.protocol import (
    MAX_MESSAGE_BYTES,
    encode_line,
    escape_tag_value,
    format_line,
    irc_lower,
    is_channel,
    parse_line,
    split_privmsg_text,
    unescape_tag_value,
    utf8_truncate,
)


class TestParseLine:
    def test_simple_command(self):
        msg = parse_line("PING :abc123")
        assert msg.command == "PING"
        assert msg.params == ["abc123"]
        assert msg.prefix is None

    def test_prefix_and_params(self):
        msg = parse_line(":nick!user@host PRIVMSG #chan :hello there")
        assert msg.prefix == "nick!user@host"
        assert msg.command == "PRIVMSG"
        assert msg.params == ["#chan", "hello there"]
        assert msg.nick == "nick"
        assert msg.trailing == "hello there"

    def test_numeric(self):
        msg = parse_line(":irc.example.com 001 tux :Welcome to IRC")
        assert msg.command == "001"
        assert msg.is_numeric
        assert msg.params == ["tux", "Welcome to IRC"]

    def test_tags(self):
        msg = parse_line("@time=2024-01-01;aaa=bbb :n!u@h PRIVMSG #c :hi")
        assert msg.tags == {"time": "2024-01-01", "aaa": "bbb"}

    def test_tag_without_value(self):
        msg = parse_line("@flag :a CMD x")
        assert msg.tags == {"flag": None}

    def test_crlf_and_bytes(self):
        msg = parse_line(b":n JOIN #chan\r\n")
        assert msg.command == "JOIN"
        assert msg.params == ["#chan"]

    def test_multiple_spaces(self):
        msg = parse_line(":s  353  tux  =  #c  :a b c")
        assert msg.command == "353"
        assert msg.params == ["tux", "=", "#c", "a b c"]

    def test_trailing_only(self):
        msg = parse_line(":s PONG :")
        assert msg.params == [""]

    def test_middle_params_with_colons(self):
        msg = parse_line(":s NOTICE * :*** Checking Ident")
        assert msg.params == ["*", "*** Checking Ident"]

    def test_empty_line(self):
        msg = parse_line("")
        assert msg.command == ""

    def test_garbage(self):
        msg = parse_line("   ")
        assert msg.command == ""

    def test_utf8_content(self):
        msg = parse_line(":n PRIVMSG #c :héllo wörld 🎉")
        assert msg.trailing == "héllo wörld 🎉"

    def test_invalid_utf8_bytes(self):
        msg = parse_line(b":n PRIVMSG #c :bad \xff\xfe bytes")
        assert msg.command == "PRIVMSG"
        assert "bytes" in msg.trailing


class TestFormatLine:
    def test_simple(self):
        assert format_line("NICK", "tux") == "NICK tux"

    def test_trailing_with_space(self):
        assert format_line("PRIVMSG", "#c", "hello world") == "PRIVMSG #c :hello world"

    def test_trailing_empty(self):
        assert format_line("TOPIC", "#c", "") == "TOPIC #c :"

    def test_trailing_colon(self):
        assert format_line("PRIVMSG", "#c", ":x") == "PRIVMSG #c ::x"

    def test_prefix_and_tags(self):
        line = format_line("PRIVMSG", "#c", "hi there", prefix="me", tags={"t": "1"})
        assert line == "@t=1 :me PRIVMSG #c :hi there"

    def test_command_uppercased(self):
        assert format_line("privmsg", "#c", "x") == "PRIVMSG #c x"

    def test_bad_middle_param(self):
        with pytest.raises(ValueError):
            format_line("CMD", "a b", "tail")

    def test_roundtrip(self):
        cases = [
            "NICK tux",
            "USER tux 0 * :termux-irc user",
            "PRIVMSG #chan :hello there",
            "JOIN #a,#b k1,k2",
            "QUIT :good bye",
        ]
        for line in cases:
            msg = parse_line(line)
            assert format_line(msg.command, *msg.params) == line


class TestEncodeLine:
    def test_crlf_appended(self):
        assert encode_line("PING x") == b"PING x\r\n"

    def test_utf8(self):
        assert encode_line("PRIVMSG #c :héllo") == "PRIVMSG #c :héllo\r\n".encode()

    def test_too_long(self):
        with pytest.raises(ValueError):
            encode_line("PRIVMSG #c :" + "x" * (MAX_MESSAGE_BYTES + 1))


class TestUtf8Truncate:
    def test_short(self):
        assert utf8_truncate("abc", 10) == "abc"

    def test_no_split_multibyte(self):
        text = "é" * 10  # 2 bytes each
        out = utf8_truncate(text, 7)
        assert len(out.encode()) <= 7
        assert out == "ééé"  # 6 bytes fits, 4th char would need 8

    def test_emoji(self):
        out = utf8_truncate("a🎉b", 2)
        assert out == "a"


class TestSplitPrivmsg:
    def test_short_text_unchanged(self):
        assert split_privmsg_text("hello") == ["hello"]

    def test_long_split(self):
        text = "word " * 200  # ~1000 bytes
        chunks = split_privmsg_text(text)
        assert len(chunks) > 1
        # Word sequence is preserved across the split.
        assert " ".join("".join(chunks).split()) == " ".join(text.split())
        for c in chunks:
            assert len(c.encode()) <= MAX_MESSAGE_BYTES - 60

    def test_unicode_split_boundaries(self):
        text = "🎉" * 300
        chunks = split_privmsg_text(text)
        for c in chunks:
            assert len(c.encode()) <= MAX_MESSAGE_BYTES - 60
        assert "".join(chunks) == text


class TestHelpers:
    def test_irc_lower(self):
        assert irc_lower("Nick{Name}") == "nick{name}"
        assert irc_lower("[A]\\~") == "{a}|^"

    def test_is_channel(self):
        assert is_channel("#chan")
        assert is_channel("&local")
        assert not is_channel("nick")

    def test_tag_escapes(self):
        assert escape_tag_value("a;b c\\d") == "a\\:b\\sc\\\\d"
        assert unescape_tag_value("a\\:b\\sc\\\\d") == "a;b c\\d"
