"""IRC protocol parsing and formatting (RFC 1459/2812 + IRCv3 message tags).

Wire format::

    [@tags] [:prefix] COMMAND [param ...] [:trailing param]

Lines are terminated by CR LF and must not exceed 512 bytes including
the terminator.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Maximum bytes on the wire including the trailing CR LF.
MAX_LINE_BYTES = 512

#: Byte budget for a single message's content (everything but CR LF).
MAX_MESSAGE_BYTES = MAX_LINE_BYTES - 2

_TAG_ESCAPES = {
    ":": ";",
    "s": " ",
    "\\": "\\",
    "r": "\r",
    "n": "\n",
}

_TAG_UNESCAPES = {v: k for k, v in _TAG_ESCAPES.items()}


def escape_tag_value(value: str) -> str:
    """Escape a string for use as an IRCv3 tag value."""
    out = []
    for ch in value:
        esc = _TAG_UNESCAPES.get(ch)
        out.append("\\" + esc if esc else ch)
    return "".join(out)


def unescape_tag_value(value: str) -> str:
    """Unescape an IRCv3 tag value."""
    if "\\" not in value:
        return value
    out = []
    it = iter(value)
    for ch in it:
        if ch == "\\":
            try:
                nxt = next(it)
            except StopIteration:
                out.append("\\")
                break
            out.append(_TAG_ESCAPES.get(nxt, nxt))
        else:
            out.append(ch)
    return "".join(out)


def parse_tags(segment: str) -> dict[str, str | None]:
    """Parse the ``@k1=v1;k2;k3=v3`` tag segment (without the ``@``)."""
    tags: dict[str, str | None] = {}
    for item in segment.split(";"):
        if not item:
            continue
        key, sep, value = item.partition("=")
        tags[key] = unescape_tag_value(value) if sep else None
    return tags


def format_tags(tags: dict[str, str | None]) -> str:
    parts = []
    for key, value in tags.items():
        parts.append(key if value is None else f"{key}={escape_tag_value(value)}")
    return ";".join(parts)


@dataclass(slots=True)
class IRCMessage:
    """A single parsed IRC protocol message."""

    command: str
    params: list[str] = field(default_factory=list)
    prefix: str | None = None
    tags: dict[str, str | None] = field(default_factory=dict)

    @property
    def trailing(self) -> str:
        """The last parameter (the free-form text part), or ``""``."""
        return self.params[-1] if self.params else ""

    @property
    def nick(self) -> str | None:
        """Nick portion of the prefix, if the prefix is a user mask."""
        if not self.prefix:
            return None
        prefix = self.prefix
        # "nick!user@host" or just "nick"; servers use "." in their names
        if "!" in prefix:
            return prefix.split("!", 1)[0]
        return prefix

    @property
    def is_numeric(self) -> bool:
        return len(self.command) == 3 and self.command.isdigit()


def parse_line(line: str | bytes) -> IRCMessage:
    """Parse one wire line (with or without the CR LF terminator)."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    line = line.rstrip("\r\n")

    tags: dict[str, str | None] = {}
    prefix: str | None = None

    if line.startswith("@"):
        tag_segment, sep, line = line.partition(" ")
        if not sep:
            # Malformed: a line consisting only of a tag segment.
            return IRCMessage(command="", tags=parse_tags(tag_segment[1:]))
        tags = parse_tags(tag_segment[1:])
        line = line.lstrip(" ")

    if line.startswith(":"):
        prefix_part, sep, line = line.partition(" ")
        if not sep:
            return IRCMessage(command="", prefix=prefix_part[1:], tags=tags)
        prefix = prefix_part[1:]
        line = line.lstrip(" ")

    params: list[str] = []
    while line:
        if line.startswith(":"):
            params.append(line[1:])
            break
        head, sep, line = line.partition(" ")
        if head:
            params.append(head)
        line = line.lstrip(" ")

    if not params:
        return IRCMessage(command="", prefix=prefix, tags=tags)
    command, rest = params[0], params[1:]
    return IRCMessage(command=command.upper(), params=rest, prefix=prefix, tags=tags)


def format_line(
    command: str,
    *params: str,
    prefix: str | None = None,
    tags: dict[str, str | None] | None = None,
) -> str:
    """Format one IRC message as a wire line (no CR LF terminator)."""
    parts: list[str] = []
    if tags:
        parts.append("@" + format_tags(tags))
    if prefix:
        parts.append(":" + prefix)
    parts.append(command.upper())

    for param in params[:-1]:
        if not param or " " in param or param.startswith(":"):
            raise ValueError(f"non-trailing parameter invalid: {param!r}")
        parts.append(param)
    if params:
        tail = params[-1]
        if not tail or " " in tail or tail.startswith(":"):
            parts.append(":" + tail)
        else:
            parts.append(tail)
    return " ".join(parts)


def encode_line(line: str) -> bytes:
    """Encode a wire line to UTF-8 bytes with the CR LF terminator.

    Raises :class:`ValueError` if the encoded line exceeds the protocol
    limit of 512 bytes.
    """
    data = line.encode("utf-8", errors="replace")
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError(f"IRC line exceeds {MAX_MESSAGE_BYTES} bytes")
    return data + b"\r\n"


def utf8_truncate(text: str, limit: int) -> str:
    """Truncate ``text`` so its UTF-8 encoding fits within ``limit`` bytes.

    Never splits a multibyte character: decoding with ``errors="ignore"``
    drops an incomplete trailing sequence.
    """
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def split_privmsg_text(text: str, overhead: int = 60) -> list[str]:
    """Split long message text into chunks that fit an IRC PRIVMSG line.

    ``overhead`` accounts for ``:nick!u@h PRIVMSG target :`` prefix bytes.
    Splits prefer word boundaries and never split a UTF-8 character.
    """
    budget = MAX_MESSAGE_BYTES - overhead
    if len(text.encode("utf-8")) <= budget:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining.encode("utf-8")) <= budget:
            chunks.append(remaining)
            break
        piece = utf8_truncate(remaining, budget)
        # Prefer splitting at the last space to keep words whole.
        space = piece.rfind(" ", max(0, len(piece) // 2))
        if space > 0:
            chunks.append(piece[:space])
            remaining = remaining[space:]
        else:
            chunks.append(piece)
            remaining = remaining[len(piece) :]
    return chunks


# RFC 1459 casemapping: {}|^ are lowercase of []\~
_CASEMAP_FROM = "[]\\~"
_CASEMAP_TO = "{}|^"
_CASEMAP_TABLE = str.maketrans(_CASEMAP_FROM, _CASEMAP_TO)


def irc_lower(name: str) -> str:
    """Case-fold an IRC nick/channel per RFC 1459 casemapping."""
    return name.translate(_CASEMAP_TABLE).lower()


def is_channel(name: str) -> bool:
    """True if ``name`` looks like an IRC channel name."""
    return name.startswith(("#", "&", "+", "!"))
