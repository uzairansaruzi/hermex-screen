"""What each RFB key means to cua-driver, whose `press_key` takes key names rather than characters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

MODIFIERS = {
    0xFFE1: "shift", 0xFFE2: "shift", 0xFFE3: "ctrl", 0xFFE4: "ctrl", 0xFFE9: "option", 0xFFEA: "option",
    0xFFEB: "cmd", 0xFFEC: "cmd", 0xFFE7: "cmd", 0xFFE8: "cmd",
}

_NAMED = {
    0xFF1B: "escape", 0xFF09: "tab", 0xFF0D: "return", 0xFF8D: "return", 0xFF08: "backspace", 0xFFFF: "delete",
    0xFF51: "left", 0xFF52: "up", 0xFF53: "right", 0xFF54: "down", 0xFF50: "home", 0xFF57: "end",
    0xFF55: "pageup", 0xFF56: "pagedown", 0x20: "space",
    **{0xFFBE + index: f"f{index + 1}" for index in range(12)},
}


@dataclass(frozen=True)
class Press:
    """A named key, with the modifiers it needs."""
    key: str
    shift: bool = False


@dataclass(frozen=True)
class Paste:
    """A character with no key name, which goes through the clipboard. That also keeps it right on
    any keyboard layout, and is how Paste and AutoFill from the phone reach a password field."""
    text: str


def action_for(keysym: int) -> Optional[Press | Paste]:
    """What a key-down of `keysym` does. None for one with no meaning here."""
    if keysym in _NAMED:
        return Press(_NAMED[keysym])
    if keysym >= 0x0100_0000:
        scalar = keysym - 0x0100_0000
    else:
        scalar = keysym
    if scalar > 0x10FFFF or scalar < 0x20 or 0x7F <= scalar < 0xA0:
        return None
    character = chr(scalar)
    if character.isascii() and character.isalpha():
        return Press(character.lower(), shift=character.isupper())
    if character.isascii() and character.isdigit():
        return Press(character)
    return Paste(character)
