"""A stand-in `cua-driver` child for tests, speaking just enough MCP over stdio. The first child
started after `break_next_child` refuses every call for its whole life, as a stuck one did; later
children work."""

import os
import stat
import sys
import tempfile
from pathlib import Path

from hermex_screen.driver import CuaDriver

_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==")

_CHILD = r'''
import json, os, sys

broken_marker = os.environ["FAKE_BROKEN_MARKER"]
broken = os.path.exists(broken_marker)
if broken:
    os.remove(broken_marker)
everything = ("scope", "button", "modifiers", "text", "on_screen_only", "pid", "window_id")
tools = [{"name": name, "inputSchema": {"properties": {key: {} for key in everything}}}
         for name in ("get_desktop_state", "click", "drag", "scroll", "press_key", "hotkey",
                      "clipboard_write", "list_windows", "bring_to_front", "check_permissions")]

def reply(request_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    message = json.loads(line)
    method, request_id = message.get("method"), message.get("id")
    if method == "initialize":
        reply(request_id, {"serverInfo": {"version": "fake"}})
    elif method == "tools/list":
        reply(request_id, {"tools": tools})
    elif method == "tools/call" and broken:
        reply(request_id, {"isError": True, "content": [{"type": "text", "text": "stuck"}]})
    elif method == "tools/call" and message["params"]["name"] == "get_desktop_state":
        reply(request_id, {"content": [{"type": "image", "data": "%s"}],
                           "structuredContent": {"screenshot_width": 1, "screenshot_height": 1}})
    elif method == "tools/call" and message["params"]["name"] == "check_permissions":
        reply(request_id, {"structuredContent": {"accessibility": True, "screen_recording": True}})
    elif request_id is not None:
        reply(request_id, {})
''' % _PNG


class FakeDriverChild:
    """Writes the fake child into a temporary directory. `driver()` is a real CuaDriver over it."""

    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name)
        self._command = root / "cua-driver"
        self._command.write_text(f"#!{sys.executable}\n{_CHILD}")
        self._command.chmod(self._command.stat().st_mode | stat.S_IXUSR)
        self._marker = root / "broken"

    def break_next_child(self) -> None:
        self._marker.touch()

    def driver(self) -> CuaDriver:
        return CuaDriver(command=str(self._command), env={**os.environ, "FAKE_BROKEN_MARKER": str(self._marker)})

    def cleanup(self) -> None:
        self._directory.cleanup()
