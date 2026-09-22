# termux-irc

A clean, asyncio-based IRC client optimized for running in [Termux](https://termux.dev/) on Android.

- **Zero runtime dependencies** — pure Python standard library (Python ≥ 3.11).
- **Curses TUI** designed for narrow mobile terminals: status bar with buffer tabs and unread counters, wrapped scrollback, editing + history on the input line, tab completion, and a plain line-mode fallback when curses can't start.
- **Real IRC protocol**: TLS or plain connections, `PASS`/`NICK`/`USER` registration, automatic `PING`/`PONG` with a keepalive watchdog, `JOIN`/`PART`, channel + private messages, `NOTICE`, CTCP `ACTION` (`/me`), `TOPIC`, `MODE` tracking, `NICK`/`QUIT`/`KICK`/`INVITE`, common server numerics (MOTD, `NAMES`, `WHOIS`, errors), nick-in-use retry, reconnect with exponential backoff, and graceful shutdown.
- **Configurable** via command-line flags, a TOML config file, and `TERMUX_IRC_*` environment variables. No credentials are ever hardcoded.

## Installation (Termux)

```sh
pkg install python git
git clone https://github.com/CommunityPokeOrg/termux-irc
cd termux-irc
```

Run it straight from the source tree — nothing to install:

```sh
python -m termux_irc irc.libera.chat --nick mynick --channel '#termux'
```

Or install it as a package to get the `termux-irc` (and `tirc`) commands:

```sh
pip install .
termux-irc irc.libera.chat
```

## Usage

```text
termux-irc [server] [options]
```

| Option | Meaning |
|---|---|
| `server` | host to connect to (optional — use `/connect` inside instead) |
| `--port PORT` | port (default: `6697` with TLS, `6667` without) |
| `--tls` / `--no-tls` | TLS on (default) or plain text |
| `--insecure` | skip TLS certificate verification (self-signed servers only) |
| `--nick NICK` | nickname |
| `--user USER` | username sent in `USER` |
| `--realname NAME` | real name sent in `USER` |
| `--password PASS` | server password (prefer `TERMUX_IRC_PASSWORD` so it stays out of shell history) |
| `--channel '#c'` | auto-join on connect (repeatable) |
| `--no-reconnect` | don't auto-reconnect on disconnect |
| `--config PATH` | alternate TOML config file |
| `--version` | print version and exit |

Starting with no server drops you into the status buffer — type
`/connect irc.libera.chat` whenever you're ready.

## Command reference

Everything is a slash command; anything else is sent as a message to the
current channel or query.

| Command | What it does |
|---|---|
| `/connect <server> [port] [tls\|plain]` | connect (aliases: `/server`) |
| `/disconnect [reason]` | hang up, stay in the client |
| `/join <#chan> [key]` (`/j`) | join a channel |
| `/part [#chan] [reason]` (`/p`) | leave a channel (defaults to current) |
| `/msg <target> <text>` | message a nick or channel |
| `/query <nick> [text]` (`/q`) | open a private-message buffer |
| `/nick <newnick>` | change nick (no args: show current) |
| `/names [#chan]` | list channel users |
| `/whois <nick>` | WHOIS query |
| `/topic [#chan] [text]` | view or set the topic |
| `/me <action>` | CTCP ACTION |
| `/notice <target> <text>` | send a NOTICE |
| `/raw <line>` (`/quote`) | raw IRC command |
| `/window [n\|name]` (`/w`, `/buffer`) | list or switch buffers |
| `/next` `/prev` | cycle buffers |
| `/close [#chan\|nick]` | close a buffer (parts if joined) |
| `/clear` | clear the current buffer |
| `/paste` | insert Android clipboard into the input line (Termux) |
| `/copy [text]` | copy text, or the last line, to the Android clipboard (Termux) |
| `/status` | connection info + battery and Wi-Fi summary (Termux) |
| `/quit [reason]` | disconnect and exit |
| `/help` (`/h`, `/?`) | in-app help |

A leading `//` sends a message that literally starts with `/`.

## Keys

| Key | Action |
|---|---|
| `Up` / `Down` | input history |
| `PgUp` / `PgDn` | scroll the buffer |
| `Ctrl-N` / `Ctrl-P` | next / previous buffer |
| `Alt-1` … `Alt-9` | jump to buffer *n* |
| `Tab` | complete nicks / slash commands |
| `Ctrl-A` / `Ctrl-E` | line start / end |
| `Ctrl-U` / `Ctrl-K` / `Ctrl-W` | clear line / kill to end / delete word |
| `Ctrl-C` | quit (sends `QUIT` first) |
| `Ctrl-D` | quit when the input line is empty |

## Configuration

Resolution order (later wins): **defaults → config file → environment → CLI flags**.

**Config file**: `~/.config/termux-irc/config.toml` (or `$XDG_CONFIG_HOME/termux-irc/config.toml`). See [`config.example.toml`](config.example.toml):

```toml
server = "irc.libera.chat"
tls = true
nick = "tux"
channels = ["#termux"]
```

**Environment variables** mirror every option with the `TERMUX_IRC_` prefix:

```sh
TERMUX_IRC_SERVER=irc.libera.chat TERMUX_IRC_NICK=tux TERMUX_IRC_PASSWORD=secret tirc
```

`TERMUX_IRC_CHANNELS="#a,#b"` accepts a comma-separated list.

### Security notes

- TLS is on by default; `--no-tls` and `--insecure` exist for testing and
  self-signed setups — use them deliberately.
- Prefer `TERMUX_IRC_PASSWORD` or the config file over `--password`, which
  lands in shell history. There is no SASL support (see limitations).
- The client never writes logs to disk and stores no credentials.

## Termux:API integration (optional)

On a real Termux install the client can talk to Android through the
[Termux:API](https://github.com/termux/termux-api) helpers — with **zero
extra Python dependencies** (they're `termux-*` command-line binaries,
invoked via `asyncio` subprocesses):

```sh
pkg install termux-api   # plus the Termux:API app from F-Droid
```

> The Termux:API Android app and the `termux-api` package must come from
> the **same source** (e.g. both F-Droid, or both GitHub releases) or the
> binaries will silently do nothing.

Once installed you get:

- **Notifications** — an Android notification when someone mentions your
  nick or sends you a private message (`termux-notification`).
- **Vibration** — the device buzzes on mentions/highlights (`termux-vibrate`).
- **Toasts** — quick pop-ups on connect/disconnect (`termux-toast`).
- **Clipboard** — `/paste` pulls `termux-clipboard-get` into your input
  line; `/copy [text]` sends text (or the last buffer line) to
  `termux-clipboard-set`.
- **System info** — `/status` adds battery (`termux-battery-status`) and
  Wi-Fi (`termux-wifi-connectioninfo`) summaries.

Everything is optional and **no-ops cleanly**: off Termux, or when a binary
is missing/times out/fails, the client behaves exactly like a plain
Linux/macOS one — nothing breaks.

### Toggles

Config file (all default `true`; `termux` is the master switch):

```toml
termux = true        # enable all Termux:API features
notify = true        # Android notifications on mentions/PMs
vibrate = true       # vibrate on mentions/highlights
toast = true         # toasts on connect/disconnect
clipboard = true     # /paste and /copy
system_info = true   # battery + wifi in /status
```

CLI: `--no-termux` disables everything; `--no-notify`, `--no-vibrate`,
`--no-toast`, `--no-clipboard`, `--no-system-info` disable one feature.
Environment: `TERMUX_IRC_NOTIFY=0`, etc.

## Troubleshooting

- **`command failed` / can't connect**: check the server and port
  (`6697` TLS / `6667` plain); try `--no-tls` for a plain-text server.
- **Certificate errors**: the server uses a self-signed cert — use
  `--insecure` only if you expect that.
- **"terminal too small" / garbled UI**: ensure `$TERM` is set (`xterm-256color`
  works); if curses can't start at all the client falls back to a plain
  line-mode interface automatically.
- **Nick in use**: the client retries with a trailing `_` automatically; pick
  another with `/nick`.
- **Keep getting disconnected**: mobile networks idle-drop TCP; the keepalive
  watchdog pings after 120 s of silence and reconnects automatically —
  `reconnect = false` or `--no-reconnect` disables that.
- **No notifications/vibrate on Termux**: install the Termux:API app **and**
  `pkg install termux-api`, both from the same source; check `/status` and
  try `termux-notification --title t --content test` by hand. Notification
  access must be granted to the Termux:API app in Android settings.

## Development

```sh
pip install pytest ruff    # dev extras only; the app itself needs nothing
python -m pytest tests/    # 131 tests: protocol, state, commands, loopback, termux-api
python -m ruff check termux_irc tests
python -m ruff format --check termux_irc tests
```

Layout:

```text
termux_irc/
  protocol.py   wire parsing/formatting, IRCv3 tags, UTF-8-safe splitting
  client.py     asyncio connection, registration, keepalive watchdog
  state.py      buffers, nick lists, unread tracking
  commands.py   /slash command dispatch
  app.py        server-message → state dispatch + reconnect supervisor
  ui.py         curses TUI (+ line-mode fallback)
  termux_api.py optional Termux:API subprocess wrapper (graceful no-op)
  config.py     file/env/CLI config
  cli.py        entry point
tests/          pytest suite (incl. a scripted loopback IRC server)
```

### Known limitations

- No SASL authentication (use server `PASS` or a bouncer for now).
- No DCC file transfer, no `CAP` negotiation, no multiserver — one network
  at a time.
- The UI is single-window; buffers (channels/queries) are switched with
  Ctrl-N/Ctrl-P, Alt-1..9, or `/window`.
- Termux:API calls are best-effort: failures and timeouts are swallowed,
  notifications can't tell whether the app is foregrounded, and there are
  no custom notification channels/icons.

## License

MIT — see [LICENSE](LICENSE).
