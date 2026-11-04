"""A client for the cua-driver Hermes itself resolves, over its stdio MCP interface, with no
overlay. Only this one driver: a version that can't do what this plugin needs produces a plain
explanation, with no workaround.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CALL_TIMEOUT = 20.0
# What each mapped operation needs from the driver's schema. Older drivers use `capture_scope`
# rather than an action-level `scope`, so they fail here rather than at the first tap.
_REQUIRED = {
    "get_desktop_state": (),
    "click": ("scope", "button"),
    "drag": ("scope",),
    "scroll": ("scope",),
    "press_key": ("scope", "modifiers"),
    "hotkey": ("scope",),
    "clipboard_write": ("text",),
    "list_windows": ("on_screen_only",),
    "bring_to_front": ("pid", "window_id"),
    "check_permissions": (),
}


class DriverUnavailable(RuntimeError):
    """The driver is missing, or can't do what this plugin needs. The message says why, plainly."""


@dataclass(frozen=True)
class Capture:
    png: bytes
    width: int
    height: int


def resolve_command() -> Optional[str]:
    """The binary Hermes selects, without installing anything."""
    from tools.computer_use.cua_backend_driver import resolve_cua_driver_cmd
    return resolve_cua_driver_cmd()


def child_env() -> dict[str, str]:
    """Hermes's own policy for spawning the driver: telemetry off, and no provider API keys."""
    from tools.computer_use.cua_backend import sanitized_cua_driver_env
    return sanitized_cua_driver_env()


def contract_problem(tools: list[dict[str, Any]]) -> Optional[str]:
    """Why this driver's schema can't serve the mapped operations, or None."""
    by_name = {tool.get("name"): tool for tool in tools}
    for name, properties in _REQUIRED.items():
        tool = by_name.get(name)
        if tool is None:
            return f"this driver has no {name} tool"
        known = (tool.get("inputSchema") or {}).get("properties") or {}
        missing = [prop for prop in properties if prop not in known]
        if missing:
            return f"this driver's {name} tool has no {', '.join(missing)} option"
    return None


class CuaDriver:
    """One `cua-driver mcp` child, started on first use. Calls take turns, because the protocol
    answers one line at a time here and the plugin applies input strictly in order."""

    def __init__(self, command: Optional[str] = None, env: Optional[dict[str, str]] = None) -> None:
        self._command = command
        self._env = env
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen] = None
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._next_id = 0
        self._tools: list[dict[str, Any]] = []
        self.version: Optional[str] = None

    def _start(self) -> None:
        command = self._command or resolve_command()
        if not command:
            raise DriverUnavailable("cua-driver isn't installed. Run `hermes computer-use install` on the host.")
        self._lines = queue.Queue()
        self._process = subprocess.Popen(
            [command, "mcp", "--no-overlay"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env=self._env if self._env is not None else child_env())
        threading.Thread(target=self._read_lines, args=(self._process, self._lines), daemon=True,
                         name="hermex-screen-driver").start()
        reply = self._request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "hermex-screen", "version": "0"}})
        self.version = ((reply.get("result") or {}).get("serverInfo") or {}).get("version")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._tools = (self._request("tools/list", {}).get("result") or {}).get("tools") or []

    @staticmethod
    def _read_lines(process: subprocess.Popen, lines: "queue.Queue[Optional[str]]") -> None:
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        assert self._process is not None and self._process.stdin is not None
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            while True:
                line = self._lines.get(timeout=_CALL_TIMEOUT)
                if line is None:
                    raise DriverUnavailable("cua-driver stopped.")
                reply = json.loads(line)
                if reply.get("id") == request_id:
                    return reply
        except (queue.Empty, OSError, ValueError) as exc:
            self.stop()
            raise DriverUnavailable("cua-driver didn't answer.") from exc

    def tools(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._start()
            return self._tools

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        """One tool call. Raises DriverUnavailable for a driver failure or a refused action."""
        with self._lock:
            self.tools()
            reply = self._request("tools/call", {"name": name, "arguments": arguments})
        result = reply.get("result") or {}
        if reply.get("error") or result.get("isError"):
            text = " ".join(part.get("text", "") for part in result.get("content") or [] if part.get("type") == "text")
            raise DriverUnavailable(text[:200] or f"cua-driver refused {name}.")
        return result

    def capture(self) -> Capture:
        result = self.call("get_desktop_state")
        for part in result.get("content") or []:
            if part.get("type") == "image":
                facts = result.get("structuredContent") or {}
                return Capture(png=base64.b64decode(part["data"]), width=int(facts.get("screenshot_width") or 0),
                               height=int(facts.get("screenshot_height") or 0))
        raise DriverUnavailable("cua-driver returned no picture.")

    def stop(self) -> None:
        with self._lock:
            if self._process is not None:
                self._process.terminate()
                self._process = None
