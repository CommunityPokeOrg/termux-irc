"""Allow running as ``python -m termux_irc``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
