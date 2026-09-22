"""Client-side state: buffers, channels, nick lists, and the active view.

The UI renders :class:`Buffer` objects; the protocol dispatcher in
:mod:`termux_irc.app` mutates them as server messages arrive.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from .protocol import irc_lower, is_channel

#: Maximum lines kept per buffer (scrollback bound).
MAX_SCROLLBACK = 5000

STATUS_BUFFER = "*status*"


@dataclass(slots=True)
class BufferLine:
    """One rendered line in a buffer."""

    kind: str  # "msg", "action", "notice", "info", "error", "highlight", "raw"
    text: str
    nick: str | None = None
    timestamp: float = field(default_factory=time.time)

    @property
    def time_str(self) -> str:
        return time.strftime("%H:%M", time.localtime(self.timestamp))


@dataclass
class Buffer:
    """A scrollback buffer: the status log, a channel, or a private query."""

    name: str
    kind: str = "status"  # "status" | "channel" | "query"
    lines: deque[BufferLine] = field(default_factory=lambda: deque(maxlen=MAX_SCROLLBACK))
    unread: int = 0
    highlight: bool = False
    topic: str = ""
    joined: bool = False
    # lnick -> membership prefix string (e.g. "@", "+", "@+")
    users: dict[str, str] = field(default_factory=dict)
    # Display names preserved for sorted rendering
    user_display: dict[str, str] = field(default_factory=dict)
    scroll: int = 0  # rows scrolled up from the bottom (UI state)

    def add(self, kind: str, text: str, nick: str | None = None) -> BufferLine:
        line = BufferLine(kind=kind, text=text, nick=nick)
        self.lines.append(line)
        return line


@dataclass
class ClientState:
    """Everything the client knows about the current IRC session."""

    nick: str = ""
    user: str = ""
    realname: str = ""
    server: str = ""
    connected: bool = False
    registered: bool = False
    quitting: bool = False
    buffers: OrderedDict[str, Buffer] = field(default_factory=OrderedDict)
    active: str = STATUS_BUFFER
    # Servers the user asked to auto-join on (re)connect
    autojoin: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.buffers[STATUS_BUFFER] = Buffer(name=STATUS_BUFFER, kind="status")

    # -- buffer helpers ----------------------------------------------------

    def buffer(self, name: str) -> Buffer:
        return self.buffers[name]

    @property
    def active_buffer(self) -> Buffer:
        return self.buffers[self.active]

    def ensure_buffer(self, name: str, kind: str | None = None) -> Buffer:
        """Return the buffer for ``name``, creating it if needed."""
        existing = self.buffers.get(name)
        if existing is not None:
            return existing
        if kind is None:
            kind = "channel" if is_channel(name) else "query"
        buf = Buffer(name=name, kind=kind)
        # Keep status first, insert others after it.
        self.buffers[name] = buf
        if name != STATUS_BUFFER and STATUS_BUFFER in self.buffers:
            self.buffers.move_to_end(STATUS_BUFFER, last=False)
        return buf

    def switch(self, name: str) -> Buffer | None:
        """Make ``name`` the active buffer; returns it, or None if unknown."""
        buf = self.buffers.get(name)
        if buf is None:
            return None
        self.active = name
        buf.unread = 0
        buf.highlight = False
        buf.scroll = 0
        return buf

    def buffer_index(self, name: str) -> int:
        return list(self.buffers).index(name)

    def next_buffer(self, delta: int = 1) -> Buffer:
        """Activate the next/previous buffer; returns it."""
        names = list(self.buffers)
        idx = names.index(self.active)
        return self.switch(names[(idx + delta) % len(names)])  # type: ignore[return-value]

    def close_buffer(self, name: str) -> None:
        if name == STATUS_BUFFER or name not in self.buffers:
            return
        del self.buffers[name]
        if self.active == name:
            self.switch(STATUS_BUFFER)

    # -- logging -----------------------------------------------------------

    def log(self, text: str, kind: str = "info", to: str | None = None) -> None:
        """Append a line to a buffer (default: the active one / status)."""
        target = to or self.active
        buf = self.ensure_buffer(target)
        buf.add(kind, text)
        if target != self.active:
            buf.unread += 1
            if kind in ("msg", "highlight"):
                buf.highlight = buf.highlight or kind == "highlight"

    # -- membership ---------------------------------------------------------

    def add_user(self, channel: str, nick: str, prefix: str = "") -> None:
        buf = self.ensure_buffer(channel, "channel")
        buf.users[irc_lower(nick)] = prefix
        buf.user_display[irc_lower(nick)] = nick

    def remove_user(self, channel: str, nick: str) -> None:
        buf = self.buffers.get(channel)
        if buf:
            lnick = irc_lower(nick)
            buf.users.pop(lnick, None)
            buf.user_display.pop(lnick, None)

    def rename_user(self, old: str, new: str) -> list[str]:
        """Rename a nick everywhere it appears; returns affected buffer names."""
        affected = []
        lold, lnew = irc_lower(old), irc_lower(new)
        for name, buf in self.buffers.items():
            if lold in buf.users:
                prefix = buf.users.pop(lold)
                buf.users[lnew] = prefix
                buf.user_display.pop(lold, None)
                buf.user_display[lnew] = new
                affected.append(name)
        # Rename a query buffer to follow the nick.
        for name in list(self.buffers):
            buf = self.buffers[name]
            if buf.kind == "query" and irc_lower(name) == lold:
                self.buffers[new] = self.buffers.pop(name)
                buf.name = new
                # Rebuild ordering
                items = list(self.buffers.items())
                self.buffers.clear()
                self.buffers.update(items)
                if self.active == name:
                    self.active = new
        return affected

    def channel_users(self, channel: str) -> list[str]:
        """Sorted display names for a channel (ops first, then voiced)."""
        buf = self.buffers.get(channel)
        if not buf:
            return []

        def sort_key(lnick: str) -> tuple[int, str]:
            p = buf.users.get(lnick, "")
            rank = 0 if "@" in p else 1 if "+" in p else 2
            return (rank, lnick)

        return [buf.user_display.get(n, n) for n in sorted(buf.users, key=sort_key)]

    def joined_channels(self) -> list[str]:
        return [n for n, b in self.buffers.items() if b.kind == "channel" and b.joined]
