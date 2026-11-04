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
This plugin adds 4002: its own timer ended this viewer's takeover, or another client handed back.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.append(_ROOT)

from hermex_screen import VERSION  # noqa: E402
from hermex_screen.control import Control, ControlLost  # noqa: E402
from hermex_screen.driver import CuaDriver, DriverUnavailable  # noqa: E402
from hermex_screen.frames import encode, fingerprint  # noqa: E402
from hermex_screen.input import InputMapper  # noqa: E402
from hermex_screen.platforms import lock_accepts_input, locked, refusal as platform_refusal, windows_opted_in  # noqa: E402
from hermex_screen.rfb import (  # noqa: E402
    SECURITY_NONE, SECURITY_OK, VERSION as RFB_VERSION, ClientParser, HandshakeReply, Key, Pointer, ProtocolError,
    UpdateRequest, desktop_size_update, jpeg_update, server_init,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_ROUTES = ["GET /capabilities", "GET /status", "GET /doctor", "POST /observe", "POST /lease/acquire",
           "POST /lease/release", "WS /stream"]
_CLOSE_CONTROL_TAKEN = 4000
_CLOSE_SCREEN_GONE = 4001
_CLOSE_HANDED_BACK = 4002
_CLOSE_BAD_TICKET = 4401
_CLOSE_PROTOCOL = 1003
_TICKET_LIFETIME = 30.0
_INPUT_QUEUE_LIMIT = 4096
_FRAME_INTERVAL = 0.15
_UNCHANGED_INTERVAL = 0.2
_CLIPBOARD_CLEAR_DELAY = 2.0

_driver = CuaDriver()
_lock = threading.Lock()
_control_instance: Optional[Control] = None
_tickets: dict[str, tuple[str, float]] = {}
_minted: set[str] = set()


def _profile_paths() -> list[str]:
    from hermes_cli.profiles import list_profiles
    return [str(profile.path) for profile in list_profiles()]


def _control() -> Control:
    """The one control object, which owns the hold file and its timer."""
    global _control_instance
    with _lock:
        if _control_instance is None:
            from hermes_constants import get_hermes_home
            from tools.bot_desktop import lease
            _control_instance = Control(lease, _profile_paths, Path(get_hermes_home()) / "hermex-screen" / "hold.json")
        return _control_instance


def _ws_authorized(ws: WebSocket) -> bool:
    from hermes_cli import web_server_chat
    return bool(web_server_chat._ws_auth_ok(ws))


def _refused(status: int, message: str, code: Optional[str] = None) -> JSONResponse:
    body: dict[str, Any] = {"detail": message}
    if code:
        body["code"] = code
    return JSONResponse(body, status_code=status)


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
    return platform_refusal() or _private_api_problem()


def _driver_facts() -> dict[str, Any]:
    """The driver's version and whether it can do the job. Starts it, which sends no input."""
    try:
        _driver.tools()
        return {"available": True, "version": _driver.version, "problem": _driver.problem}
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
    return JSONResponse({"plugin_version": VERSION, "routes": _ROUTES, "windows_opt_in": windows_opted_in()})


@router.get("/status")
def get_status() -> JSONResponse:
    refusal = _control_problem()
    body: dict[str, Any] = {"plugin_version": VERSION, "supported": refusal is None, "refusal": refusal, "platform": sys.platform}
    if refusal is None:
        driver = _driver_facts()
        body.update(driver=driver, permissions=_permissions() if driver["available"] and not driver["problem"] else None,
                    locked=locked(), locked_input=lock_accepts_input(), lease=_control().public_lease())
    return JSONResponse(body)


@router.get("/doctor")
def get_doctor() -> JSONResponse:
    checks: list[dict[str, Any]] = []

    def check(name: str, problem: Optional[str]) -> None:
        checks.append({"name": name, "ok": problem is None, "detail": problem})

    check("platform", platform_refusal())
    check("hermes_apis", _private_api_problem())
    check("isolation", _isolation_problem())
    driver = _driver_facts()
    check("driver", driver["problem"])
    if driver["available"] and not driver["problem"]:
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
    ticket = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _lock:
        _minted.add(viewer_id)
        for stale in [key for key, (_, expires) in _tickets.items() if expires < now]:
            del _tickets[stale]
        _tickets[ticket] = (viewer_id, now + _TICKET_LIFETIME)
    return JSONResponse({"viewer_id": viewer_id, "stream_ticket": ticket, "lease": _control().public_lease()})


@router.post("/lease/acquire")
def post_acquire(body: dict[str, Any] = Body(default={})) -> JSONResponse:
    if refusal := _control_problem():
        return _refused(403, refusal)
    viewer_id = str(body.get("viewer_id") or "").strip()
    with _lock:
        minted = viewer_id in _minted
    if not minted:
        return _refused(409, "viewer_id was not minted by /observe.", code="viewer_mismatch")
    if _driver_facts()["problem"]:
        return _refused(403, "This host's driver can't do the job, so control stays with the bot.")
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
    with _lock:
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
        self._handed_back = asyncio.Event()
        # Bounded so a long paste on a slow host pushes back on the socket rather than piling up here.
        self._input: asyncio.Queue[object] = asyncio.Queue(maxsize=_INPUT_QUEUE_LIMIT)
        self._mapper = InputMapper(_driver, sys.platform, guard=lambda: self._control.while_holding(viewer_id))
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
        was_holder, self._held_ever = self._held_ever, False
        if was_holder and holder is not None:
            self._loop.call_soon_threadsafe(self._evicted.set)
        elif was_holder and expired:
            self._loop.call_soon_threadsafe(self._handed_back.set)

    async def _watch_eviction(self) -> None:
        taken = asyncio.create_task(self._evicted.wait())
        ended = asyncio.create_task(self._handed_back.wait())
        await asyncio.wait({taken, ended}, return_when=asyncio.FIRST_COMPLETED)
        for task in (taken, ended):
            task.cancel()
        if self._evicted.is_set():
            await self._ws.close(code=_CLOSE_CONTROL_TAKEN, reason="control-taken")
        else:
            await self._ws.close(code=_CLOSE_HANDED_BACK, reason="handed-back")

    async def _read(self) -> None:
        parser = ClientParser()
        while True:
            message = await self._ws.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is None:
                await self._ws.close(code=_CLOSE_PROTOCOL, reason="RFB is binary")
                return
            try:
                messages = parser.feed(data)
            except ProtocolError as exc:
                await self._ws.close(code=_CLOSE_PROTOCOL, reason=str(exc)[:100])
                return
            for item in messages:
                if isinstance(item, HandshakeReply):
                    await self._ws.send_bytes({
                        "security": SECURITY_NONE, "security_result": SECURITY_OK,
                        "server_init": server_init(self._size[0], self._size[1])}[item.stage])
                elif isinstance(item, UpdateRequest):
                    self._wanted.set()
                elif isinstance(item, (Key, Pointer)) and self._control.may_send_input(self._viewer_id):
                    self._control.note_input()
                    await self._input.put(item)

    async def _apply_input(self) -> None:
        while True:
            events = [await self._input.get()]
            while not self._input.empty():
                events.append(self._input.get_nowait())
            if not self._control.may_send_input(self._viewer_id):
                continue
            try:
                await asyncio.to_thread(self._mapper.apply, events)
            except ControlLost:
                continue
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
            try:
                capture = await asyncio.to_thread(_driver.capture)
            except (DriverUnavailable, OSError):
                # Not a clean close: the viewer reads /status again to say what went wrong.
                await self._ws.close(code=_CLOSE_SCREEN_GONE, reason="capture failed")
                return
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
