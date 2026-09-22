"""Tests for the Termux:API integration — all subprocesses mocked."""

from __future__ import annotations

import asyncio
import json

import pytest

from termux_irc.app import IRCApp, handle_message
from termux_irc.commands import CommandContext, dispatch_input
from termux_irc.config import Config
from termux_irc.protocol import parse_line
from termux_irc.state import STATUS_BUFFER, ClientState
from termux_irc.termux_api import (
    TermuxAPI,
    binary_available,
    format_battery,
    format_wifi,
    on_termux,
)


class FakeProc:
    """Stand-in for asyncio.subprocess.Process."""

    def __init__(self, stdout=b"", returncode=0, hang=False):
        self._stdout = stdout
        self.returncode = returncode
        self.hang = hang
        self.killed = False

    async def communicate(self):
        if self.hang:
            await asyncio.sleep(60)
        return self._stdout, b""

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        self.hang = False
        return self.returncode


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _all_binaries_present(monkeypatch):
    """Pretend every termux-* binary is on PATH unless a test overrides."""
    monkeypatch.setattr("shutil.which", lambda b: f"/data/data/com.termux/{b}")


def make_api(**kw) -> TermuxAPI:
    kw.setdefault("_on_termux", True)
    return TermuxAPI(**kw)


# -- detection ---------------------------------------------------------------


def test_on_termux_env(monkeypatch):
    monkeypatch.setenv("TERMUX_VERSION", "0.118")
    monkeypatch.delenv("PREFIX", raising=False)
    assert on_termux()


def test_on_termux_prefix(monkeypatch):
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    assert on_termux()


def test_not_on_termux(monkeypatch):
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.setenv("PREFIX", "/usr")
    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.setenv("PREFIX", "/usr/local")
    assert not on_termux()


def test_binary_available(monkeypatch):
    assert binary_available("termux-notification")
    monkeypatch.setattr("shutil.which", lambda b: None)
    assert not binary_available("termux-notification")


def test_active_only_on_termux():
    assert make_api().active
    assert not make_api(_on_termux=False).active
    assert not make_api(enabled=False).active


def test_usable_checks_binary(monkeypatch):
    api = make_api()
    monkeypatch.setattr("shutil.which", lambda b: "/x" if b == "ok" else None)
    assert api.usable("ok")
    assert not api.usable("missing")
    off = make_api(enabled=False)
    assert not off.usable("ok")


# -- _exec -------------------------------------------------------------------


def test_exec_success(monkeypatch):
    api = make_api()
    seen = {}

    async def fake_exec(binary, *args, **kw):
        seen["call"] = (binary, args)
        return FakeProc(stdout=b"hi\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    ok, out = run(api._exec("termux-test", "a", "b"))
    assert ok and out == "hi"
    assert seen["call"] == ("termux-test", ("a", "b"))


def test_exec_bad_returncode(monkeypatch):
    api = make_api()
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        lambda *a, **k: _fake_coro(FakeProc(returncode=1)),
    )
    ok, out = run(api._exec("termux-test"))
    assert not ok and out == ""


async def _fake_coro(value):
    return value


def test_exec_spawn_failure(monkeypatch):
    api = make_api()

    async def boom(*a, **k):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    ok, _ = run(api._exec("missing"))
    assert not ok


def test_exec_timeout(monkeypatch):
    api = make_api(timeout=0.05)
    proc = FakeProc(hang=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: _fake_coro(proc))
    ok, _ = run(api._exec("slow"))
    assert not ok
    assert proc.killed


# -- feature methods ---------------------------------------------------------


def test_notification_args(monkeypatch):
    api = make_api()
    calls = []

    async def fake(binary, *args, **k):
        calls.append((binary, args))
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    assert run(api.send_notification("hi", "there"))
    binary, args = calls[0]
    assert binary == "termux-notification"
    assert "--title" in args and "hi" in args
    assert "--content" in args and "there" in args


def test_vibrate_and_toast(monkeypatch):
    api = make_api()
    calls = []

    async def fake(binary, *args, **k):
        calls.append(binary)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    assert run(api.do_vibrate())
    assert run(api.show_toast("yo"))
    assert calls == ["termux-vibrate", "termux-toast"]


def test_clipboard_roundtrip(monkeypatch):
    api = make_api()

    async def fake(binary, *args, **k):
        if binary == "termux-clipboard-get":
            return FakeProc(stdout=b"clip text\n")
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    assert run(api.clipboard_get()) == "clip text"
    assert run(api.clipboard_set("x"))


def test_battery_json(monkeypatch):
    api = make_api()
    payload = json.dumps({"percentage": 73, "plugged": "PLUGGED_USB"})

    async def fake(binary, *args, **k):
        return FakeProc(stdout=payload.encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    info = run(api.battery_status())
    assert info["percentage"] == 73


def test_battery_bad_json(monkeypatch):
    api = make_api()
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        lambda *a, **k: _fake_coro(FakeProc(stdout=b"not json")),
    )
    assert run(api.battery_status()) is None


def test_wifi_json(monkeypatch):
    api = make_api()
    payload = json.dumps({"ssid": '"home"', "bssid": "aa:bb", "ip": "10.0.0.2"})
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        lambda *a, **k: _fake_coro(FakeProc(stdout=payload.encode())),
    )
    info = run(api.wifi_info())
    assert info["ip"] == "10.0.0.2"


def test_methods_noop_when_inactive(monkeypatch):
    api = make_api(_on_termux=False)
    called = []

    async def fake(binary, *args, **k):
        called.append(binary)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    assert not run(api.send_notification("a", "b"))
    assert not run(api.do_vibrate())
    assert not run(api.show_toast("x"))
    assert run(api.clipboard_get()) is None
    assert not run(api.clipboard_set("x"))
    assert run(api.battery_status()) is None
    assert run(api.wifi_info()) is None
    assert called == []


def test_methods_noop_when_binary_missing(monkeypatch):
    api = make_api()
    monkeypatch.setattr("shutil.which", lambda b: None)
    assert not run(api.send_notification("a", "b"))
    assert not run(api.do_vibrate())
    assert run(api.clipboard_get()) is None


def test_per_feature_toggles(monkeypatch):
    api = make_api(notify=False)
    monkeypatch.setattr("shutil.which", lambda b: f"/x/{b}")
    called = []

    async def fake(binary, *args, **k):
        called.append(binary)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    assert not run(api.send_notification("a", "b"))  # notify off
    assert run(api.do_vibrate())  # vibrate still on
    assert called == ["termux-vibrate"]


def test_ping_user_fans_out(monkeypatch):
    api = make_api()
    monkeypatch.setattr("shutil.which", lambda b: f"/x/{b}")
    called = []

    async def fake(binary, *args, **k):
        called.append(binary)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    run(api.ping_user("t", "c"))
    assert "termux-notification" in called and "termux-vibrate" in called


def test_format_helpers():
    assert "57%" in format_battery(
        {"percentage": 57, "plugged": "PLUGGED_USB", "health": "GOOD"}
    )
    assert (
        "unplugged"
        not in format_battery({"percentage": 57, "plugged": "PLUGGED_USB"}).lower()
    )
    assert "net" in format_wifi({"ssid": '"net"', "ip": "10.0.0.1"})
    assert format_battery({}) == "battery: unknown"
    assert format_wifi({}) == "wifi: unknown"


# -- app wiring --------------------------------------------------------------


def make_app(**kw) -> IRCApp:
    cfg = Config()
    for k, v in kw.items():
        setattr(cfg, k, v)
    return IRCApp(cfg)


def feed(app: IRCApp, line: str) -> None:
    handle_message(app, parse_line(line))


def collect(api):
    """Make api.ping_user record (title, content)."""
    api.pinged = []

    async def ping(title, content):
        api.pinged.append((title, content))

    api.ping_user = ping  # type: ignore[method-assign]
    api.toasted = []

    async def toast(text):
        api.toasted.append(text)

    api.show_toast = toast  # type: ignore[method-assign]
    api._on_termux = True
    return api


def test_highlight_pings(monkeypatch):
    app = make_app()
    api = collect(app.termux)
    monkeypatch.setattr("shutil.which", lambda b: f"/x/{b}")

    async def main():
        task = asyncio.get_running_loop()
        feed(app, ":bob!u@h PRIVMSG #chan :hey tux look")
        # let the scheduled ping run
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return task

    run(main())
    assert api.pinged, "mention should ping"
    title, content = api.pinged[0]
    assert "bob" in title and "#chan" in title


def test_pm_pings():
    app = make_app()
    api = collect(app.termux)

    async def main():
        feed(app, ":bob!u@h PRIVMSG tux :hello there")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    run(main())
    assert api.pinged and "private" in api.pinged[0][0]


def test_normal_channel_msg_no_ping():
    app = make_app()
    api = collect(app.termux)

    async def main():
        feed(app, ":bob!u@h PRIVMSG #chan :nobody mentioned")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    run(main())
    assert not api.pinged


def test_own_msg_no_ping():
    app = make_app()
    api = collect(app.termux)

    async def main():
        feed(app, ":tux!u@h PRIVMSG tux :self highlight tux")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    run(main())
    assert not api.pinged


def test_no_ping_off_termux():
    app = make_app()
    api = collect(app.termux)
    api._on_termux = False
    assert not api.active
    feed(app, ":bob!u@h PRIVMSG tux :hi")
    assert not api.pinged


def test_ping_failure_never_raises():
    app = make_app()
    app.termux._on_termux = True

    async def boom(title, content):
        raise RuntimeError("subprocess died")

    app.termux.ping_user = boom  # type: ignore[method-assign]

    async def main():
        feed(app, ":bob!u@h PRIVMSG tux :yo")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    run(main())  # no exception propagates


# -- commands ----------------------------------------------------------------


def make_ctx() -> CommandContext:
    state = ClientState(nick="tux", user="tux", realname="r")
    calls: dict[str, list] = {"connect": [], "disconnect": [], "quit": []}
    ctx = CommandContext(
        state=state,
        connect=lambda h, p, t: calls["connect"].append((h, p, t)) or _none(),
        disconnect=lambda r: calls["disconnect"].append(r) or _none(),
        quit=lambda r: calls["quit"].append(r) or _none(),
    )
    ctx.calls = calls  # type: ignore[attr-defined]
    return ctx


async def _none():
    return None


def test_cmd_paste_into_input():
    ctx = make_ctx()
    ctx.termux = make_api()
    inserted = []
    ctx.insert_text = inserted.append

    ctx.termux.clipboard_get = lambda: _coro("multi\nline paste")  # type: ignore[assignment]
    run(dispatch_input(ctx, "/paste"))
    assert inserted == ["multi line paste"]
    assert any("pasted" in ln.text for ln in ctx.state.active_buffer.lines)


async def _coro(v):
    return v


def test_cmd_paste_unavailable():
    ctx = make_ctx()
    ctx.termux = make_api(_on_termux=False)
    run(dispatch_input(ctx, "/paste"))
    assert any("unavailable" in ln.text for ln in ctx.state.active_buffer.lines)


def test_cmd_paste_no_hook_shows_text():
    ctx = make_ctx()
    ctx.termux = make_api()
    ctx.termux.clipboard_get = lambda: _coro("clip stuff")  # type: ignore[assignment]
    run(dispatch_input(ctx, "/paste"))
    assert any("clip stuff" in ln.text for ln in ctx.state.active_buffer.lines)


def test_cmd_copy_text():
    ctx = make_ctx()
    ctx.termux = make_api()
    copied = []
    ctx.termux.clipboard_set = lambda t: copied.append(t) or _coro(True)  # type: ignore[assignment]
    run(dispatch_input(ctx, "/copy hello world"))
    assert copied == ["hello world"]


def test_cmd_copy_last_line():
    ctx = make_ctx()
    ctx.termux = make_api()
    copied = []
    ctx.termux.clipboard_set = lambda t: copied.append(t) or _coro(True)  # type: ignore[assignment]
    ctx.state.log("previous message", to=STATUS_BUFFER)
    run(dispatch_input(ctx, "/copy"))
    assert copied == ["previous message"]


def test_cmd_copy_unavailable():
    ctx = make_ctx()
    ctx.termux = make_api(clipboard=False)
    run(dispatch_input(ctx, "/copy hi"))
    assert any("unavailable" in ln.text for ln in ctx.state.active_buffer.lines)


def test_cmd_status_shows_battery_wifi():
    ctx = make_ctx()
    ctx.termux = make_api()
    ctx.termux.battery_status = lambda: _coro(  # type: ignore[assignment]
        {"percentage": 88, "plugged": "UNPLUGGED"}
    )
    ctx.termux.wifi_info = lambda: _coro({"ssid": '"net"', "ip": "10.0.0.5"})  # type: ignore[assignment]
    run(dispatch_input(ctx, "/status"))
    text = "\n".join(ln.text for ln in ctx.state.active_buffer.lines)
    assert "88%" in text and "net" in text


def test_cmd_status_no_termux():
    ctx = make_ctx()
    ctx.termux = make_api(_on_termux=False)
    ctx.state.connected = True
    ctx.state.server = "irc.example"
    run(dispatch_input(ctx, "/status"))
    text = "\n".join(ln.text for ln in ctx.state.active_buffer.lines)
    assert "connected" in text
    # no battery/wifi lines
    assert "%" not in text


def test_cmd_status_system_info_off():
    ctx = make_ctx()
    ctx.termux = make_api(system_info=False)
    run(dispatch_input(ctx, "/status"))
    text = "\n".join(ln.text for ln in ctx.state.active_buffer.lines)
    assert "not connected" in text and "%" not in text
