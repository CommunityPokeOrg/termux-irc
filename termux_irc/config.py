"""Configuration: defaults < config file < environment < CLI flags.

Config file is TOML at ``$XDG_CONFIG_HOME/termux-irc/config.toml``
(``~/.config/termux-irc/config.toml`` by default). Environment variables
use the ``TERMUX_IRC_`` prefix, e.g. ``TERMUX_IRC_NICK``,
``TERMUX_IRC_SERVER``. Passwords should come from the file or the
environment — never hardcoded, and prefer env vars over CLI flags so
they don't land in shell history.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ENV_PREFIX = "TERMUX_IRC_"

DEFAULT_CONFIG_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "termux-irc"
    / "config.toml"
)


@dataclass
class Config:
    server: str | None = None
    port: int | None = None  # resolved from tls default when unset
    tls: bool = True
    tls_verify: bool = True
    nick: str = "tux"
    user: str = "tux"
    realname: str = "termux-irc user"
    password: str | None = None
    channels: list[str] = field(default_factory=list)
    reconnect: bool = True
    ping_after: float = 120.0
    ping_timeout: float = 60.0

    @property
    def resolved_port(self) -> int:
        if self.port is not None:
            return self.port
        return 6697 if self.tls else 6667


def _coerce(cfg: Config, key: str, value: object) -> None:
    """Set a config field with light type coercion for env/file values."""
    if not hasattr(cfg, key):
        raise KeyError(key)
    current = getattr(cfg, key)
    if isinstance(current, bool):
        if isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes", "on")
        else:
            value = bool(value)
    elif isinstance(current, int) and not isinstance(current, bool):
        value = int(value)  # type: ignore[arg-type]
    elif isinstance(current, float):
        value = float(value)  # type: ignore[arg-type]
    elif isinstance(current, list):
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        else:
            value = list(value)  # type: ignore[arg-type]
    elif value is not None:
        value = str(value)
    setattr(cfg, key, value)


_FILE_KEYS = {
    "server",
    "port",
    "tls",
    "tls_verify",
    "nick",
    "user",
    "realname",
    "password",
    "channels",
    "reconnect",
    "ping_after",
    "ping_timeout",
}

_ENV_KEYS = _FILE_KEYS


def load_file(path: Path | None = None) -> dict[str, object]:
    """Load the TOML config file; returns {} when absent."""
    path = path or DEFAULT_CONFIG_PATH
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in _FILE_KEYS}


def load_env(environ: dict[str, str] | None = None) -> dict[str, object]:
    """Collect TERMUX_IRC_* environment variables."""
    environ = os.environ if environ is None else environ
    out: dict[str, object] = {}
    for key in _ENV_KEYS:
        env_name = ENV_PREFIX + key.upper()
        if env_name in environ:
            out[key] = environ[env_name]
    return out


def build_config(
    cli_overrides: dict[str, object] | None = None,
    environ: dict[str, str] | None = None,
    config_path: Path | None = None,
) -> Config:
    """Merge defaults < file < env < CLI into a :class:`Config`."""
    cfg = Config()
    for source in (load_file(config_path), load_env(environ), cli_overrides or {}):
        for key, value in source.items():
            if value is None:
                continue
            try:
                _coerce(cfg, key, value)
            except (KeyError, ValueError, TypeError):
                continue
    return cfg


EXAMPLE_CONFIG = """\
# termux-irc configuration
# Copy to ~/.config/termux-irc/config.toml and edit.

server = "irc.libera.chat"
# port = 6697                # default: 6697 with tls, 6667 without
tls = true                  # set false for plaintext
# tls_verify = true         # set false only for self-signed servers
nick = "tux"
user = "tux"
realname = "termux-irc user"
# password = "server-password"   # prefer TERMUX_IRC_PASSWORD env instead
channels = ["#termux"]
reconnect = true
"""
