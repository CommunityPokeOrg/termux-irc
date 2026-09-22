"""Optional Termux:API integration — zero external Python dependencies.

Wraps the ``termux-*`` command-line tools (installed by the Termux:API
Android app + ``pkg install termux-api``) via asyncio subprocess. Every
call degrades gracefully: off Termux, missing binaries, timeouts, and
non-zero exits all no-op instead of raising, so standard Linux/macOS
behavior is preserved.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

#: Default per-subprocess timeout (seconds).
DEFAULT_TIMEOUT = 5.0

_TERMUX_DIR_MARKERS = ("/data/data/com.termux",)


def on_termux(environ: dict[str, str] | None = None) -> bool:
    """True when running inside a Termux environment."""
    environ = os.environ if environ is None else environ
    if environ.get("TERMUX_VERSION"):
        return True
    prefix = environ.get("PREFIX", "")
    if any(marker in prefix for marker in _TERMUX_DIR_MARKERS):
        return True
    return False


def binary_available(name: str) -> bool:
    """True if the ``termux-*`` binary is on PATH."""
    return shutil.which(name) is not None


@dataclass
class TermuxAPI:
    """Graceful async wrapper around termux-api binaries.

    Feature toggles come from config; ``enabled`` is the master switch.
    When a toggle is off or the binary is missing the method is a no-op.
    """

    enabled: bool = True
    notify: bool = True
    vibrate: bool = True
    toast: bool = True
    clipboard: bool = True
    system_info: bool = True
    timeout: float = DEFAULT_TIMEOUT
    # Resolved at construction; tests may override.
    _on_termux: bool | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._on_termux is None:
            self._on_termux = on_termux()

    @property
    def active(self) -> bool:
        """True when the integration can actually do something."""
        return self.enabled and bool(self._on_termux)

    def usable(self, binary: str) -> bool:
        """True when this feature is enabled and the binary exists."""
        return self.active and binary_available(binary)

    async def _exec(
        self, binary: str, *args: str, timeout: float | None = None
    ) -> tuple[bool, str]:
        """Run a termux-* binary. Returns (ok, stdout) — never raises."""
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, _err = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout or self.timeout
                )
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
                    await proc.wait()
                return False, ""
        except (OSError, TimeoutError):
            return False, ""
        if proc.returncode != 0:
            return False, ""
        return True, out.decode("utf-8", errors="replace").strip()

    # -- individual features -------------------------------------------------

    async def send_notification(self, title: str, content: str) -> bool:
        """Post an Android notification via termux-notification."""
        if not (self.notify and self.usable("termux-notification")):
            return False
        ok, _ = await self._exec(
            "termux-notification",
            "--title",
            title,
            "--content",
            content,
            "--priority",
            "high",
        )
        return ok

    async def do_vibrate(self, duration_ms: int = 300) -> bool:
        """Buzz the device via termux-vibrate."""
        if not (self.vibrate and self.usable("termux-vibrate")):
            return False
        ok, _ = await self._exec("termux-vibrate", "-d", str(int(duration_ms)))
        return ok

    async def show_toast(self, text: str, long: bool = False) -> bool:
        """Show a quick toast via termux-toast."""
        if not (self.toast and self.usable("termux-toast")):
            return False
        args = ["termux-toast"]
        if long:
            args.append("-l")
        args.append(text)
        ok, _ = await self._exec(*args)
        return ok

    async def clipboard_get(self) -> str | None:
        """Read the device clipboard; None when unavailable."""
        if not (self.clipboard and self.usable("termux-clipboard-get")):
            return None
        ok, out = await self._exec("termux-clipboard-get")
        return out if ok else None

    async def clipboard_set(self, text: str) -> bool:
        """Write to the device clipboard via termux-clipboard-set."""
        if not (self.clipboard and self.usable("termux-clipboard-set")):
            return False
        ok, _ = await self._exec("termux-clipboard-set", text)
        return ok

    async def battery_status(self) -> dict[str, Any] | None:
        """Parsed battery info from termux-battery-status (JSON)."""
        if not (self.system_info and self.usable("termux-battery-status")):
            return None
        ok, out = await self._exec("termux-battery-status")
        if not ok:
            return None
        try:
            return json.loads(out)
        except (ValueError, TypeError):
            return None

    async def wifi_info(self) -> dict[str, Any] | None:
        """Parsed Wi-Fi info from termux-wifi-connectioninfo (JSON)."""
        if not (self.system_info and self.usable("termux-wifi-connectioninfo")):
            return None
        ok, out = await self._exec("termux-wifi-connectioninfo")
        if not ok:
            return None
        try:
            return json.loads(out)
        except (ValueError, TypeError):
            return None

    # -- composite helpers ---------------------------------------------------

    async def ping_user(self, title: str, content: str) -> None:
        """Notify + vibrate for a mention/highlight, fire-and-forget safe."""
        await self.send_notification(title, content)
        await self.do_vibrate()


def format_battery(info: dict[str, Any]) -> str:
    """Human-readable one-liner for termux-battery-status JSON."""
    parts = []
    pct = info.get("percentage")
    if pct is not None:
        parts.append(f"{pct}%")
    plugged = info.get("plugged", "")
    if plugged and plugged != "UNPLUGGED":
        parts.append(plugged.lower().replace("_", " "))
    status = info.get("status", "")
    if status:
        parts.append(status.lower())
    temp = info.get("temperature")
    if temp is not None:
        parts.append(f"{temp}°C")
    return "battery: " + " ".join(parts) if parts else "battery: unknown"


def format_wifi(info: dict[str, Any]) -> str:
    """Human-readable one-liner for termux-wifi-connectioninfo JSON."""
    if not info:
        return "wifi: unknown"
    ssid = info.get("ssid", "?")
    rssi = info.get("rssi")
    speed = info.get("link_speed_mbps") or info.get("link_speed")
    parts = [f"wifi: {ssid}"]
    if rssi is not None:
        parts.append(f"{rssi}dBm")
    if speed is not None:
        parts.append(f"{speed}Mbps")
    ip = info.get("ip")
    if ip:
        parts.append(str(ip))
    return " ".join(parts)
