"""hermex-screen's routes, under /api/plugins/hermex-screen/. Mounted by the dashboard, behind its
own gate. A plugin can add REST routes and sockets but no RPC methods, so everything is here.

    GET  /capabilities       {plugin_version, routes, windows_opt_in}, like hermex-push's.
    GET  /status             Driver, permissions, lock state, display size and the lease.
    GET  /doctor             Pass or fail for each requirement. Sends no input.
    POST /observe            {viewer_id, stream_ticket}: a new id, and a single-use 30 s ticket.
    POST /lease/acquire      {viewer_id}. Take Over across every Profile on the host.
    POST /lease/release      {viewer_id?, force?}. Hand Back. Stock's rules: an id releases only
                             itself, and force with no id clears any holder.
    WS   /stream             RFB, as a stock host's /api/display/ws. `?ticket=` or `?token=` passes
                             the dashboard's gate, and `?stream_ticket=` names the viewer.

Close codes match stock: 4000 control taken, 4001 screen gone, 4401 bad ticket, 1003 protocol error.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import subprocess
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.append(_ROOT)

from hermex_screen import VERSION  # noqa: E402
from hermex_screen.control import Control, viewer_hash  # noqa: E402
from hermex_screen.driver import CuaDriver, DriverUnavailable, contract_problem  # noqa: E402
from hermex_screen.frames import encode, fingerprint  # noqa: E402
from hermex_screen.input import InputMapper  # noqa: E402
from hermex_screen.rfb import (  # noqa: E402
    SECURITY_NONE, SECURITY_OK, VERSION as RFB_VERSION, ClientParser, Key, Pointer, ProtocolError, UpdateRequest,
    desktop_size_update, jpeg_update, server_init,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_ROUTES = ["GET /capabilities", "GET /status", "GET /doctor", "POST /observe", "POST /lease/acquire",
           "POST /lease/release", "WS /stream"]
_CLOSE_CONTROL_TAKEN = 4000
_CLOSE_SCREEN_GONE = 4001
_CLOSE_BAD_TICKET = 4401
_CLOSE_PROTOCOL = 1003
_TICKET_LIFETIME = 30.0
_FRAME_INTERVAL = 0.15
_UNCHANGED_INTERVAL = 0.2
_CLIPBOARD_CLEAR_DELAY = 2.0
_WINDOWS_OPT_IN_ENV = "HERMEX_SCREEN_WINDOWS"

_driver = CuaDriver()
_tickets: dict[str, tuple[str, float]] = {}
_minted: set[str] = set()


def _windows_opted_in() -> bool:
    import os
    return os.environ.get(_WINDOWS_OPT_IN_ENV, "").strip() == "1"


def _platform_refusal() -> Optional[str]:
    """Why this plugin won't serve this host. Linux has stock Bot Screen."""
    if sys.platform == "darwin":
        return None
    if sys.platform == "win32":
        return None if _windows_opted_in() else (
            f"Windows isn't supported yet. A tester can set {_WINDOWS_OPT_IN_ENV}=1 for the dashboard to opt in.")
    return "This host has stock Bot Screen, so it doesn't use this plugin."


def _profile_paths() -> list[str]:
    from hermes_cli.profiles import list_profiles
    return [str(profile.path) for profile in list_profiles()]


@lru_cache(maxsize=1)
def _control() -> Control:
    from hermes_constants import get_hermes_home
    from tools.bot_desktop import lease
    return Control(lease, _profile_paths, Path(get_hermes_home()) / "hermex-screen" / "hold.json")


def _ws_authorized(ws: WebSocket) -> bool:
    from hermes_cli import web_server_chat
    return bool(web_server_chat._ws_auth_ok(ws))


def _refused(status: int, message: str, code: Optional[str] = None) -> JSONResponse:
    body: dict[str, Any] = {"detail": message}
    if code:
        body["code"] = code
    return JSONResponse(body, status_code=status)


def _locked() -> Optional[bool]:
    """Whether the console session's screen is locked. None where this can't tell."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-n", "Root", "-d1"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return "CGSSessionScreenIsLocked" in out


def _isolation_problem() -> Optional[str]:
    from hermes_cli.config import load_config_readonly
    isolation = ((load_config_readonly() or {}).get("plugins") or {}).get("isolation") or "in_process"
    if isolation != "in_process":
        return "The stream needs plugins.isolation: in_process, because forwarded plugin routes carry no sockets."
    return None


def _private_api_problem() -> Optional[str]:
    """Control is refused if the private Hermes APIs it relies on have changed."""
    try:
        from hermes_cli.profiles import list_profiles
        from tools.bot_desktop import lease
    except ImportError as exc:
        return f"A Hermes API this plugin needs is missing: {exc.name}."
    for name in ("acquire", "release", "get", "public_view"):
        if not callable(getattr(lease, name, None)):
            return f"A Hermes API this plugin needs is missing: lease.{name}."
    if not callable(list_profiles):
        return "A Hermes API this plugin needs is missing: list_profiles."
    return None


def _control_problem() -> Optional[str]:
    return _platform_refusal() or _private_api_problem()


def _driver_facts() -> dict[str, Any]:
    """The driver's version and whether it can do the job. Starts it, which sends no input."""
    try:
        problem = contract_problem(_driver.tools())
        return {"available": True, "version": _driver.version, "problem": problem}
    except DriverUnavailable as exc:
        return {"available": False, "version": None, "problem": str(exc)}
    except Exception as exc:
        logger.exception("hermex-screen: driver")
        return {"available": False, "version": None, "problem": f"The driver couldn't start: {type(exc).__name__}."}


def _permissions() -> Optional[dict[str, Any]]:
    try:
        facts = _driver.call("check_permissions").get("structuredContent") or {}
    except DriverUnavailable:
        return None
    return {"accessibility": facts.get("accessibility"), "screen_recording": facts.get("screen_recording")}


@router.get("/capabilities")
def get_capabilities() -> JSONResponse:
    return JSONResponse({"plugin_version": VERSION, "routes": _ROUTES, "windows_opt_in": _windows_opted_in()})


@router.get("/status")
def get_status() -> JSONResponse:
    refusal = _control_problem()
    body: dict[str, Any] = {"plugin_version": VERSION, "supported": refusal is None, "refusal": refusal, "platform": sys.platform}
    if refusal is None:
        driver = _driver_facts()
        body.update(driver=driver, permissions=_permissions() if driver["available"] else None, locked=_locked(),
                    lease=_control().public_lease())
    return JSONResponse(body)


@router.get("/doctor")
def get_doctor() -> JSONResponse:
    checks: list[dict[str, Any]] = []

    def check(name: str, problem: Optional[str]) -> None:
        checks.append({"name": name, "ok": problem is None, "detail": problem})

    check("platform", _platform_refusal())
    check("hermes_apis", _private_api_problem())
    check("isolation", _isolation_problem())
    driver = _driver_facts()
    check("driver", driver["problem"])
    if driver["available"]:
        permissions = _permissions() or {}
        check("accessibility", None if permissions.get("accessibility") else "Accessibility isn't granted to CuaDriver.")
        check("screen_recording", None if permissions.get("screen_recording") else "Screen Recording isn't granted to CuaDriver.")
    try:
        from PIL import Image  # noqa: F401
        check("pillow", None)
    except ImportError:
        check("pillow", "Pillow is missing, and frames can't be encoded without it.")
    return JSONResponse({"plugin_version": VERSION, "ok": all(item["ok"] for item in checks),
                         "driver_version": driver["version"], "checks": checks})


@router.post("/observe")
def post_observe() -> JSONResponse:
    if refusal := _control_problem():
        return _refused(403, refusal)
    viewer_id = secrets.token_urlsafe(16)
    _minted.add(viewer_id)
    ticket = secrets.token_urlsafe(24)
    _prune_tickets()
    _tickets[ticket] = (viewer_id, time.monotonic() + _TICKET_LIFETIME)
    return JSONResponse({"viewer_id": viewer_id, "stream_ticket": ticket, "lease": _control().public_lease()})


def _prune_tickets() -> None:
    now = time.monotonic()
    for ticket in [key for key, (_, expires) in _tickets.items() if expires < now]:
        del _tickets[ticket]


@router.post("/lease/acquire")
def post_acquire(body: dict[str, Any] = Body(default={})) -> JSONResponse:
    if refusal := _control_problem():
        return _refused(403, refusal)
    viewer_id = str(body.get("viewer_id") or "").strip()
    if viewer_id not in _minted:
        return _refused(409, "viewer_id was not minted by /observe.", code="viewer_mismatch")
    _control().acquire(viewer_id)
    return JSONResponse({"lease": _control().public_lease()})


@router.post("/lease/release")
def post_release(body: dict[str, Any] = Body(default={})) -> JSONResponse:
    if refusal := _private_api_problem():
        return _refused(403, refusal)
    viewer_id = str(body.get("viewer_id") or "").strip() or None
    if viewer_id is None and not body.get("force"):
        return _refused(409, "viewer_id required, or pass force: true.", code="viewer_mismatch")
    _control().release(viewer_id)
    return JSONResponse({"lease": _control().public_lease()})


@router.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    if not _ws_authorized(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    entry = _tickets.pop(ws.query_params.get("stream_ticket", ""), None)
    if entry is None or entry[1] < time.monotonic():
        await ws.close(code=_CLOSE_BAD_TICKET, reason="ticket missing, expired or used")
        return
    if _control_problem():
        await ws.close(code=_CLOSE_SCREEN_GONE, reason="this host refuses")
        return
    await _Stream(ws, entry[0]).run()


class _Stream:
    """One viewer. A reader parses its bytes, a worker applies input in order, and the writer sends
    a frame when one is wanted and the screen has changed."""

    def __init__(self, ws: WebSocket, viewer_id: str) -> None:
        self._ws = ws
        self._viewer_id = viewer_id
        self._control = _control()
        self._loop = asyncio.get_running_loop()
        self._wanted = asyncio.Event()
        self._evicted = asyncio.Event()
        self._input: asyncio.Queue[object] = asyncio.Queue()
        self._mapper = InputMapper(_driver, sys.platform)
        self._held_ever = self._control.may_send_input(viewer_id)
        self._clear_clipboard: Optional[asyncio.TimerHandle] = None
        self._size = (0, 0)

    async def run(self) -> None:
        try:
            first = await asyncio.to_thread(_driver.capture)
        except (DriverUnavailable, OSError):
            await self._ws.close(code=_CLOSE_SCREEN_GONE, reason="capture failed")
            return
        frame = await asyncio.to_thread(encode, first.png, None)
        self._size = (frame.width, frame.height)
        await asyncio.to_thread(self._set_scale, frame)
        unsubscribe = self._control.subscribe(self._on_control)
        tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._write(frame)),
                 asyncio.create_task(self._apply_input()), asyncio.create_task(self._watch_eviction())]
        try:
            await self._ws.send_bytes(RFB_VERSION)
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, ConnectionError, RuntimeError)):
                    logger.debug("hermex-screen stream ended: %r", exc)
        finally:
            unsubscribe()
            if self._clear_clipboard is not None:
                self._clear_clipboard.cancel()
            await asyncio.to_thread(self._flush_clipboard)
            try:
                await self._ws.close()
            except Exception:
                pass

    def _set_scale(self, frame: Any) -> None:
        self._mapper.scale = frame.source_width / frame.width
        try:
            facts = _driver.call("get_desktop_state").get("structuredContent") or {}
            self._mapper.point_scale = float(facts.get("scale_factor") or 1.0)
        except DriverUnavailable:
            pass

    def _on_control(self, holder: Optional[str], expired: bool) -> None:
        if holder == self._viewer_id:
            self._held_ever = True
            return
        if self._held_ever and (holder is not None or expired):
            self._held_ever = False
            self._loop.call_soon_threadsafe(self._evicted.set)

    async def _watch_eviction(self) -> None:
        await self._evicted.wait()
        await self._ws.close(code=_CLOSE_CONTROL_TAKEN, reason="control-taken")

    async def _read(self) -> None:
        parser = ClientParser()
        sent = 0
        total = 0
        while True:
            message = await self._ws.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is None:
                await self._ws.close(code=_CLOSE_PROTOCOL, reason="RFB is binary")
                return
            total += len(data)
            # The fixed handshake: a version answer, the security choice, then ClientInit.
            if sent == 0 and total >= 12:
                await self._ws.send_bytes(SECURITY_NONE)
                sent = 1
            if sent == 1 and total >= 13:
                await self._ws.send_bytes(SECURITY_OK)
                sent = 2
            if sent == 2 and total >= 14:
                await self._send_init()
                sent = 3
            try:
                messages = parser.feed(data)
            except ProtocolError as exc:
                await self._ws.close(code=_CLOSE_PROTOCOL, reason=str(exc)[:100])
                return
            for item in messages:
                if isinstance(item, UpdateRequest):
                    self._wanted.set()
                elif isinstance(item, (Key, Pointer)) and self._control.may_send_input(self._viewer_id):
                    self._control.note_input()
                    self._input.put_nowait(item)

    async def _send_init(self) -> None:
        await self._ws.send_bytes(server_init(self._size[0], self._size[1]))

    async def _apply_input(self) -> None:
        while True:
            events = [await self._input.get()]
            while not self._input.empty():
                events.append(self._input.get_nowait())
            if not self._control.may_send_input(self._viewer_id):
                continue
            try:
                await asyncio.to_thread(self._mapper.apply, events)
            except DriverUnavailable as exc:
                logger.info("hermex-screen: input refused: %s", exc)
            if self._mapper.pasted:
                if self._clear_clipboard is not None:
                    self._clear_clipboard.cancel()
                self._clear_clipboard = self._loop.call_later(
                    _CLIPBOARD_CLEAR_DELAY, lambda: asyncio.ensure_future(asyncio.to_thread(self._flush_clipboard)))

    def _flush_clipboard(self) -> None:
        try:
            self._mapper.clear_clipboard()
        except DriverUnavailable:
            pass

    async def _write(self, first: Any) -> None:
        last_print = None
        while True:
            await self._wanted.wait()
            started = self._loop.time()
            pointer = self._mapper.pointer if self._control.may_send_input(self._viewer_id) else None
            capture = await asyncio.to_thread(_driver.capture)
            print_ = fingerprint(capture.png, pointer)
            if print_ == last_print:
                await asyncio.sleep(_UNCHANGED_INTERVAL)
                continue
            frame = await asyncio.to_thread(encode, capture.png, pointer)
            if (frame.width, frame.height) != self._size:
                self._size = (frame.width, frame.height)
                await self._ws.send_bytes(desktop_size_update(frame.width, frame.height))
                await asyncio.to_thread(self._set_scale, frame)
            last_print = print_
            self._wanted.clear()
            await self._ws.send_bytes(jpeg_update(frame.width, frame.height, frame.jpeg))
            elapsed = self._loop.time() - started
            if elapsed < _FRAME_INTERVAL:
                await asyncio.sleep(_FRAME_INTERVAL - elapsed)


def _resume_a_saved_hold() -> None:
    """Building the control object reads a hold left by a restart, whose timer then carries on, so a
    restart mid-takeover still ends in a hand-back with no phone connected."""
    if _control_problem() is None:
        _control()


threading.Thread(target=_resume_a_saved_hold, daemon=True, name="hermex-screen-startup").start()
