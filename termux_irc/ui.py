"""Curses terminal UI: status bar, scrollback, input line.

Designed for narrow mobile terminals (Termux): a single status row, a
wrapped scrollback area, and a one-line input field. Integrates with the
asyncio event loop via ``loop.add_reader`` on stdin.
"""

from __future__ import annotations

import asyncio
import curses
import sys
from collections import deque
from collections.abc import Awaitable, Callable

from .state import STATUS_BUFFER, BufferLine, ClientState

CTRL_A, CTRL_C, CTRL_D, CTRL_E = 1, 3, 4, 5
CTRL_K, CTRL_L, CTRL_N, CTRL_P = 11, 12, 14, 16
CTRL_U, CTRL_W = 21, 23
ESC = 27
BACKSPACE = {8, 127, curses.KEY_BACKSPACE}


class ChatUI:
    """Interactive curses chat interface bound to an asyncio loop."""

    def __init__(
        self,
        state: ClientState,
        submit: Callable[[str], Awaitable[None]],
        request_quit: Callable[[], None],
    ) -> None:
        self.state = state
        self.submit = submit
        self.request_quit = request_quit
        self.running = False

        self._stdscr: curses.window | None = None
        self._input = ""
        self._cursor = 0
        self._history: deque[str] = deque(maxlen=200)
        self._hist_idx: int | None = None
        self._hist_stash = ""
        self._completion: tuple[list[str], int, int] | None = None
        self._dirty = True
        self._colors_ok = False
        self._nick_color_base = 0
        self._esc_pending = False

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        """Run the UI until :meth:`stop` is called."""
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            await self._run_line_mode()
            return
        try:
            stdscr = curses.initscr()
            curses.raw()
            curses.noecho()
            stdscr.keypad(True)
            stdscr.nodelay(True)
            if curses.has_colors():
                curses.start_color()
                try:
                    curses.use_default_colors()
                except curses.error:
                    pass
                for i in range(1, 8):
                    try:
                        curses.init_pair(i, i, -1)
                    except curses.error:
                        pass
                self._colors_ok = True
            try:
                curses.curs_set(1)
            except curses.error:
                pass
            self._stdscr = stdscr
        except curses.error:
            # Could not init the terminal (bad $TERM, etc.) — fall back to
            # a plain line-mode interface.
            self._stdscr = None
            await self._run_line_mode()
            return
        self.running = True
        loop = asyncio.get_running_loop()
        loop.add_reader(sys.stdin.fileno(), self._read_input)
        try:
            while self.running:
                if self._dirty:
                    self._draw()
                await asyncio.sleep(0.05)
        finally:
            loop.remove_reader(sys.stdin.fileno())
            self._curses_exit()

    def _curses_exit(self) -> None:
        if self._stdscr is None:
            return
        try:
            self._stdscr.keypad(False)
            self._stdscr.nodelay(False)
            curses.curs_set(1)
            curses.nocbreak()
            curses.echo()
            curses.endwin()
        except curses.error:
            pass
        self._stdscr = None

    def stop(self) -> None:
        self.running = False

    def invalidate(self) -> None:
        """Mark the display dirty; called after state changes."""
        self._dirty = True

    # -- line-mode fallback -------------------------------------------------

    async def _run_line_mode(self) -> None:
        """Minimal stdin/stdout REPL for when curses cannot start."""
        self.running = True
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str] = asyncio.Queue()

        def feed() -> None:
            line = sys.stdin.readline()
            loop.call_soon_threadsafe(queue.put_nowait, line)

        print("termux-irc (line mode — curses unavailable). /help for commands.")
        loop.add_reader(sys.stdin.fileno(), feed)
        shown: dict[str, int] = {}
        try:
            while self.running:
                # Flush new lines from every buffer.
                for name, buf in self.state.buffers.items():
                    seen = shown.get(name, 0)
                    for line in list(buf.lines)[seen:]:
                        print(f"[{line.time_str}] [{name}] {self._flatten(line)}")
                    shown[name] = len(buf.lines)
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=0.5)
                except TimeoutError:
                    continue
                if line == "":  # EOF / Ctrl-D
                    self.request_quit()
                    break
                await self.submit(line.rstrip("\n"))
        finally:
            loop.remove_reader(sys.stdin.fileno())

    @staticmethod
    def _flatten(line: BufferLine) -> str:
        if line.nick is not None:
            return f"<{line.nick}> {line.text}"
        return line.text

    # -- input handling ------------------------------------------------------

    def _read_input(self) -> None:
        stdscr = self._stdscr
        if stdscr is None:
            return
        keys: list[int | str] = []
        while True:
            try:
                ch = stdscr.get_wch()
            except curses.error:
                break
            keys.append(ch)
            if len(keys) > 64:
                break
        for ch in keys:
            self._handle_key(ch)
        self._dirty = True

    def _handle_key(self, ch: int | str) -> None:
        # get_wch returns str for printable input, int for special keys.
        code = ord(ch) if isinstance(ch, str) and len(ch) == 1 else ch

        if self._esc_pending:
            self._esc_pending = False
            if isinstance(code, int) and ord("1") <= code <= ord("9"):
                self._goto_buffer(code - ord("1"))
            return

        if isinstance(ch, str) and ch.isprintable():
            self._insert(ch)
            return

        if ch == curses.KEY_RESIZE:
            return  # redraw happens via _dirty
        if code == ESC:
            self._esc_pending = True
            return
        if code == CTRL_C or (code == CTRL_D and not self._input):
            self.request_quit()
            return
        if code == CTRL_D:
            self._del_char()
        elif code in BACKSPACE:
            self._backspace()
        elif code in (CTRL_A, curses.KEY_HOME):
            self._cursor = 0
        elif code in (CTRL_E, curses.KEY_END):
            self._cursor = len(self._input)
        elif code == CTRL_U:
            self._input = ""
            self._cursor = 0
        elif code == CTRL_K:
            self._input = self._input[: self._cursor]
        elif code == CTRL_W:
            self._delete_word()
        elif code == CTRL_L:
            self._dirty = True
        elif code == CTRL_N:
            self.state.next_buffer(1)
        elif code == CTRL_P:
            self.state.next_buffer(-1)
        elif code == curses.KEY_LEFT:
            self._cursor = max(0, self._cursor - 1)
        elif code == curses.KEY_RIGHT:
            self._cursor = min(len(self._input), self._cursor + 1)
        elif code == curses.KEY_UP:
            self._history_move(-1)
        elif code == curses.KEY_DOWN:
            self._history_move(1)
        elif code == curses.KEY_PPAGE:
            self._scroll(+10)
        elif code == curses.KEY_NPAGE:
            self._scroll(-10)
        elif code == curses.KEY_DC:
            self._del_char()
        elif code == ord("\t"):
            self._complete()
        elif code in (10, 13, curses.KEY_ENTER):
            self._submit_line()
        elif isinstance(code, int) and 32 <= code:
            # Non-printable stray or a byte of a wide char: insert what fits.
            try:
                self._insert(chr(code))
            except ValueError:
                pass

    def _goto_buffer(self, idx: int) -> None:
        names = list(self.state.buffers)
        if 0 <= idx < len(names):
            self.state.switch(names[idx])

    def _scroll(self, delta: int) -> None:
        buf = self.state.active_buffer
        buf.scroll = max(0, buf.scroll + delta)

    # -- input editing --------------------------------------------------------

    def _insert(self, text: str) -> None:
        self._completion = None
        self._input = self._input[: self._cursor] + text + self._input[self._cursor :]
        self._cursor += len(text)

    def _backspace(self) -> None:
        if self._cursor > 0:
            self._input = self._input[: self._cursor - 1] + self._input[self._cursor :]
            self._cursor -= 1

    def _del_char(self) -> None:
        if self._cursor < len(self._input):
            self._input = self._input[: self._cursor] + self._input[self._cursor + 1 :]

    def _delete_word(self) -> None:
        i = self._cursor
        while i > 0 and self._input[i - 1] == " ":
            i -= 1
        while i > 0 and self._input[i - 1] != " ":
            i -= 1
        self._input = self._input[:i] + self._input[self._cursor :]
        self._cursor = i

    def _history_move(self, delta: int) -> None:
        if not self._history:
            return
        if self._hist_idx is None:
            self._hist_stash = self._input
            self._hist_idx = len(self._history) if delta < 0 else 0
        self._hist_idx += delta
        self._hist_idx = max(self._hist_idx, 0)
        if self._hist_idx >= len(self._history):
            self._hist_idx = None
            self._input = self._hist_stash
        else:
            self._input = self._history[self._hist_idx]
        self._cursor = len(self._input)

    def _submit_line(self) -> None:
        line = self._input
        self._input = ""
        self._cursor = 0
        self._hist_idx = None
        self._completion = None
        if line.strip():
            self._history.append(line)
        asyncio.get_running_loop().create_task(self.submit(line))

    # -- tab completion --------------------------------------------------------

    def _complete(self) -> None:
        """Complete slash commands at word 0, nicks elsewhere."""
        buf = self.state.active_buffer
        if self._completion is not None:
            options, start, n = self._completion
            self._completion = (options, start, (n + 1) % len(options))
            self._apply_completion()
            return
        head = self._input[: self._cursor]
        start = head.rfind(" ") + 1
        word = head[start:]
        if not word:
            return
        if start == 0 and word.startswith("/"):
            from .commands import COMMANDS  # noqa: PLC0415 (lazy: avoids cycle)

            options = sorted(c for c in COMMANDS if c.startswith(word[1:]))
            options = ["/" + c for c in options]
        else:
            nicks = []
            if buf.kind == "channel":
                nicks = list(buf.user_display.values())
            nicks += [n for n in self.state.buffers if n != STATUS_BUFFER]
            options = [n for n in nicks if n.lower().startswith(word.lower())]
            if start == 0:
                options = [o + ":" for o in options]
        if not options:
            return
        self._completion = (sorted(set(options)), start, 0)
        self._apply_completion()

    def _apply_completion(self) -> None:
        assert self._completion is not None
        options, start, n = self._completion
        word = options[n % len(options)]
        self._input = self._input[:start] + word + self._input[self._cursor :]
        self._cursor = start + len(word)

    # -- rendering -------------------------------------------------------------

    def _attr(self, kind: str, nick: str | None = None) -> int:
        if not self._colors_ok:
            return curses.A_BOLD if kind == "highlight" else 0
        if kind == "error":
            return curses.color_pair(1) | curses.A_BOLD
        if kind == "highlight":
            return curses.color_pair(3) | curses.A_BOLD
        if kind == "info":
            return curses.color_pair(6)
        if kind == "notice":
            return curses.color_pair(5)
        if kind == "action":
            return curses.color_pair(4)
        if kind == "raw":
            return curses.color_pair(2)
        return 0

    def _nick_attr(self, nick: str) -> int:
        if not self._colors_ok:
            return curses.A_BOLD
        palette = (2, 3, 4, 5, 6, 7)
        idx = (hash(nick.lower()) % len(palette)) + 2
        return curses.color_pair(idx) | curses.A_BOLD

    def _line_segments(self, line: BufferLine) -> list[tuple[str, int]]:
        """Render one BufferLine to (text, attr) segments."""
        ts = f"{line.time_str} "
        dim = curses.A_DIM if self._colors_ok else 0
        segs: list[tuple[str, int]] = [(ts, dim)]
        attr = self._attr(line.kind, line.nick)
        if line.nick is not None:
            if line.kind == "action":
                segs.append(("* ", attr))
                segs.append((line.nick, self._nick_attr(line.nick)))
                segs.append((" " + line.text, attr))
            else:
                segs.append(("<", attr))
                segs.append((line.nick, self._nick_attr(line.nick)))
                segs.append(("> ", attr))
                segs.append((line.text, attr))
        else:
            segs.append((line.text, attr))
        return segs

    #: Indent for continuation rows — matches the "HH:MM " timestamp prefix.
    _WRAP_INDENT = " " * 9

    @classmethod
    def _wrap_segments(
        cls, segs: list[tuple[str, int]], width: int
    ) -> list[list[tuple[str, int]]]:
        """Wrap styled segments to ``width`` columns, preserving attrs."""
        width = max(width, 8)
        chars: list[str] = []
        attrs: list[int] = []
        for s, a in segs:
            chars.extend(s)
            attrs.extend([a] * len(s))
        indent_attr = segs[0][1] if segs else 0
        n = len(chars)
        rows: list[list[tuple[str, int]]] = []
        i = 0
        first = True
        while i < n or not rows:
            budget = width if first else width - len(cls._WRAP_INDENT)
            end = i + budget
            if end >= n:
                row_chars, row_attrs = chars[i:], attrs[i:]
                i = n
            else:
                # Break at the last space within budget; hard-break if none.
                j = end
                while j > i and chars[j - 1] != " ":
                    j -= 1
                if j == i:
                    j = end
                row_chars, row_attrs = chars[i:j], attrs[i:j]
                i = j
                while i < n and chars[i] == " ":
                    i += 1
            row: list[tuple[str, int]] = []
            if not first:
                row.append((cls._WRAP_INDENT, indent_attr))
            for c, a in zip(row_chars, row_attrs, strict=True):
                if row and row[-1][1] == a:
                    row[-1] = (row[-1][0] + c, a)
                else:
                    row.append((c, a))
            rows.append(row)
            first = False
        return rows

    def _draw(self) -> None:
        stdscr = self._stdscr
        if stdscr is None:
            return
        self._dirty = False
        try:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            if height < 3 or width < 10:
                stdscr.addnstr(0, 0, "terminal too small", width - 1)
                stdscr.refresh()
                return
            self._draw_status(stdscr, width)
            body_top, body_bot = 1, height - 2
            self._draw_buffer(stdscr, body_top, body_bot, width)
            self._draw_input(stdscr, height - 1, width)
            stdscr.refresh()
        except curses.error:
            pass

    def _draw_status(self, stdscr: curses.window, width: int) -> None:
        state = self.state
        conn = state.server if state.connected else "offline"
        left = f" {state.nick}@{conn}" if state.connected else " termux-irc"
        tabs = []
        for i, name in enumerate(state.buffers):
            buf = state.buffers[name]
            label = f"{i + 1}:{name}"
            if buf.unread:
                label += f"({buf.unread})"
            if buf.highlight:
                label += "!"
            if name == state.active:
                label = f"[{label}]"
            tabs.append(label)
        right = " ".join(tabs)
        line = left + " | " + right if width >= 40 else right
        line = line[: width - 1].ljust(width - 1)
        attr = curses.A_REVERSE
        try:
            stdscr.addnstr(0, 0, line, width - 1, attr)
        except curses.error:
            pass

    def _draw_buffer(self, stdscr: curses.window, top: int, bot: int, width: int) -> None:
        buf = self.state.active_buffer
        rows_avail = bot - top + 1
        # Build wrapped rows bottom-up.
        all_rows: list[list[tuple[str, int]]] = []
        for line in buf.lines:
            all_rows.extend(self._wrap_segments(self._line_segments(line), width))
        total = len(all_rows)
        max_scroll = max(0, total - rows_avail)
        buf.scroll = min(buf.scroll, max_scroll)
        end = total - buf.scroll
        start = max(0, end - rows_avail)
        y = bot
        for row in reversed(all_rows[start:end]):
            if y < top:
                break
            x = 0
            for text, attr in row:
                room = width - 1 - x
                if room <= 0:
                    break
                try:
                    stdscr.addnstr(y, x, text, room, attr)
                except curses.error:
                    pass
                x += len(text)
            y -= 1
        if buf.scroll > 0:
            try:
                stdscr.addnstr(
                    top, 0, f"-- scrolled up {buf.scroll} --", width - 1, curses.A_DIM
                )
            except curses.error:
                pass

    def _draw_input(self, stdscr: curses.window, y: int, width: int) -> None:
        buf = self.state.active_buffer
        prompt = f"{buf.name}> " if buf.name != STATUS_BUFFER else "> "
        max_x = width - 1
        text = prompt + self._input
        cursor_abs = len(prompt) + self._cursor
        # Scroll input horizontally so the cursor is visible.
        offset = max(0, cursor_abs - max_x + 1)
        visible = text[offset : offset + max_x]
        try:
            stdscr.addnstr(y, 0, visible.ljust(max_x), max_x)
            stdscr.move(y, cursor_abs - offset)
        except curses.error:
            pass
