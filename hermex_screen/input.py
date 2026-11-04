"""Applies RFB input to the desktop through cua-driver, strictly in order.

A key goes to the frontmost window, so before a tap lands the window under it comes forward. Before
later keys, an app that has since taken the front gives way to the tapped window again, or the keys
are dropped if it's gone, so a password can't reach a different app than the one tapped. A press and release in one place is a
click; a press that moved is a drag; wheel buttons scroll.

Pasting is asynchronous: the app reads the clipboard when it handles the shortcut, so the next
character can't go on the clipboard straight away. A run of them pastes together, then waits. A
wheel gesture's clicks also merge, because each driver scroll takes about 300 ms.
"""

from __future__ import annotations

import time
from contextlib import nullcontext
from typing import Any, Callable, ContextManager, Optional

from .driver import DriverUnavailable
from .keys import MODIFIERS, Paste, Press, action_for
from .rfb import Key, Pointer

_LEFT, _MIDDLE, _RIGHT, _WHEEL_UP, _WHEEL_DOWN = 1, 2, 4, 8, 16
_DRAG_SLOP = 4
_BUTTONS = {_LEFT: "left", _MIDDLE: "middle", _RIGHT: "right"}
_WHEELS = {_WHEEL_UP: "up", _WHEEL_DOWN: "down"}
_PASTE_SETTLE = 0.15
_DRIVER_APP = "Cua Driver"


class InputMapper:
    """`scale` turns framebuffer pixels into the driver's screenshot pixels, and `point_scale` those
    into the points `list_windows` reports. `guard()` wraps each driver call that acts on the
    desktop, and raises to stop the rest of a batch once this viewer has lost control."""

    def __init__(self, driver: Any, platform: str, sleep: Callable[[float], None] = time.sleep,
                 guard: Callable[[], ContextManager[None]] = nullcontext) -> None:
        self._driver = driver
        self._sleep = sleep
        self._guard = guard
        self._target: Optional[dict[str, Any]] = None
        # The tapped window couldn't come back, so keys wait for a new tap rather than go elsewhere.
        self._keys_blocked = False
        self._paste_modifier = "ctrl" if platform == "win32" else "cmd"
        self.scale = 1.0
        self.point_scale = 1.0
        self.pointer: Optional[tuple[int, int]] = None
        self._held: set[str] = set()
        self._buttons = 0
        self._pressed_at: Optional[tuple[int, int]] = None
        self.pasted = False

    def _act(self, name: str, **arguments: Any) -> dict[str, Any]:
        with self._guard():
            return self._driver.call(name, **arguments)

    def _keys_may_land(self) -> bool:
        """Puts the tapped window back in front if another app has taken it, before a key goes out.
        With no tap yet, keys go to whatever the picture shows in front."""
        if self._keys_blocked:
            return False
        if self._target is None:
            return True
        result = self._driver.call("list_windows", on_screen_only=True)
        windows = [window for window in (result.get("structuredContent") or {}).get("windows") or []
                   if _is_ordinary(window)]
        front = max(windows, key=lambda item: item.get("z_index") or 0, default=None)
        if front is not None and front.get("pid") == self._target["pid"]:
            return True
        try:
            self._act("bring_to_front", pid=self._target["pid"], window_id=self._target["window_id"])
            return True
        except DriverUnavailable:
            self._keys_blocked = True
            return False

    def apply(self, events: list[object]) -> None:
        """Applies events in order, as one batch, so what piled up during a slow call can merge."""
        # Asked at the first key after each tap, since a tap in this same batch changes the answer.
        keys_may_land: Optional[bool] = None
        pasting = ""
        scrolling: Optional[list[Any]] = None

        def flush() -> None:
            nonlocal pasting, scrolling
            if pasting:
                self._paste(pasting)
                pasting = ""
            if scrolling is not None:
                x, y = self._true(scrolling[1], scrolling[2])
                self._act("scroll", direction=scrolling[0], amount=scrolling[3], x=x, y=y, scope="desktop")
                scrolling = None

        for event in events:
            if isinstance(event, Key):
                if event.down and event.keysym not in MODIFIERS:
                    if keys_may_land is None:
                        flush()
                        keys_may_land = self._keys_may_land()
                    if not keys_may_land:
                        continue
                action = action_for(event.keysym) if event.keysym not in MODIFIERS else None
                if isinstance(action, Paste):
                    if event.down:
                        pasting += action.text
                    continue
                if event.keysym not in MODIFIERS and not event.down:
                    continue
                flush()
                self._key(event)
            elif isinstance(event, Pointer):
                wheel = next((mask for mask in _WHEELS if event.buttons & ~self._buttons & mask), None)
                if wheel is not None:
                    self._buttons = event.buttons
                    self.pointer = (event.x, event.y)
                    if scrolling is None or scrolling[0] != _WHEELS[wheel]:
                        flush()
                        scrolling = [_WHEELS[wheel], event.x, event.y, 0]
                    scrolling[3] += 1
                elif self._buttons & ~event.buttons & (_WHEEL_UP | _WHEEL_DOWN) and event.buttons == 0:
                    self._buttons = 0
                else:
                    flush()
                    self._pointer(event)
                    keys_may_land = None
        flush()

    def _true(self, x: int, y: int) -> tuple[float, float]:
        return x * self.scale, y * self.scale

    def _key(self, event: Key) -> None:
        if event.keysym in MODIFIERS:
            (self._held.add if event.down else self._held.discard)(MODIFIERS[event.keysym])
            return
        if not event.down:
            return
        action = action_for(event.keysym)
        if isinstance(action, Press):
            modifiers = set(self._held)
            if action.shift:
                modifiers.add("shift")
            self._act("press_key", key=action.key, modifiers=sorted(modifiers), scope="desktop")

    def _paste(self, text: str) -> None:
        self._act("clipboard_write", text=text)
        self._act("hotkey", keys=[self._paste_modifier, "v"], scope="desktop")
        self.pasted = True
        self._sleep(_PASTE_SETTLE)

    def clear_clipboard(self) -> None:
        """Pasted characters can be a password's, so they don't stay on this computer's clipboard."""
        if self.pasted:
            self.pasted = False
            self._driver.call("clipboard_write", text="")

    def _pointer(self, event: Pointer) -> None:
        pressed = event.buttons & ~self._buttons
        released = self._buttons & ~event.buttons
        self._buttons = event.buttons
        self.pointer = (event.x, event.y)
        if pressed & (_LEFT | _RIGHT | _MIDDLE):
            self._pressed_at = (event.x, event.y)
        for mask, button in _BUTTONS.items():
            if released & mask:
                self._release(button, event)

    def _release(self, button: str, event: Pointer) -> None:
        start = self._pressed_at or (event.x, event.y)
        self._pressed_at = None
        x, y = self._true(event.x, event.y)
        from_x, from_y = self._true(*start)
        self._bring_forward(from_x, from_y)
        modifiers = sorted(self._held)
        extra = {"modifier": modifiers} if modifiers else {}
        if abs(event.x - start[0]) > _DRAG_SLOP or abs(event.y - start[1]) > _DRAG_SLOP:
            self._act("drag", from_x=from_x, from_y=from_y, to_x=x, to_y=y, button=button,
                              scope="desktop", duration_ms=200, steps=8, **extra)
        else:
            self._act("click", x=x, y=y, button=button, scope="desktop", **extra)

    def _bring_forward(self, x: float, y: float) -> None:
        """Brings the topmost window under a screenshot-pixel point forward, if it isn't already."""
        px, py = x / self.point_scale, y / self.point_scale
        result = self._driver.call("list_windows", on_screen_only=True)
        windows = (result.get("structuredContent") or {}).get("windows") or []
        under = [window for window in windows if _contains(window, px, py)]
        self._keys_blocked = False
        if not under:
            self._target = None
            return
        window = max(under, key=lambda item: item.get("z_index") or 0)
        self._act("bring_to_front", pid=window["pid"], window_id=window["window_id"])
        self._target = {"pid": window["pid"], "window_id": window["window_id"]}


def _is_ordinary(window: dict[str, Any]) -> bool:
    """An app's window on screen. The driver's own full-screen surface sits in the list too."""
    return window.get("layer") in (0, None) and window.get("is_on_screen") is not False \
        and window.get("pid") is not None and window.get("app_name") != _DRIVER_APP


def _contains(window: dict[str, Any], x: float, y: float) -> bool:
    if not _is_ordinary(window):
        return False
    bounds = window.get("bounds") or {}
    left, top = bounds.get("x", 0.0), bounds.get("y", 0.0)
    return left <= x < left + bounds.get("width", 0.0) and top <= y < top + bounds.get("height", 0.0)
