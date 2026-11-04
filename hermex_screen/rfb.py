"""The server side of the RFB the Hermex viewer already reads from a stock host: version 3.8,
security type None, and one Tight JPEG rectangle per update. Client bytes don't align with
WebSocket frames, so the parser takes chunks of any size.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

VERSION = b"RFB 003.008\n"
SECURITY_NONE = bytes([1, 1])
SECURITY_OK = bytes(4)
_HANDSHAKE = 12 + 1 + 1
# After the version, the security choice and ClientInit, in the client's bytes still to come.
_HANDSHAKE_REPLIES = {2: "security", 1: "security_result", 0: "server_init"}
_MAX_CUT_TEXT = 256 * 1024

_FIXED = {0: 20, 3: 10, 4: 8, 5: 6, 150: 10, 255: 12}


@dataclass(frozen=True)
class HandshakeReply:
    """The client reached this point in the handshake, so the server's next part is due."""
    stage: str


@dataclass(frozen=True)
class UpdateRequest:
    incremental: bool


@dataclass(frozen=True)
class Key:
    down: bool
    keysym: int


@dataclass(frozen=True)
class Pointer:
    buttons: int
    x: int
    y: int


class ProtocolError(ValueError):
    """A message that can't be framed. Nothing after it can be read, so the stream closes."""


class ClientParser:
    """Feed client bytes; get back the messages this server acts on, in order."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._handshake_left = _HANDSHAKE

    def feed(self, chunk: bytes) -> list[object]:
        self._buffer += chunk
        messages: list[object] = []
        while self._handshake_left and self._buffer:
            # A step at a time, so each reply goes out when its own byte has arrived.
            step = self._handshake_left - 2 if self._handshake_left > 2 else 1
            taken = min(step, len(self._buffer))
            del self._buffer[:taken]
            self._handshake_left -= taken
            if self._handshake_left in _HANDSHAKE_REPLIES:
                messages.append(HandshakeReply(_HANDSHAKE_REPLIES[self._handshake_left]))
        if self._handshake_left:
            return messages
        while self._buffer:
            length = self._length()
            if length is None or len(self._buffer) < length:
                break
            message = bytes(self._buffer[:length])
            del self._buffer[:length]
            parsed = _parse(message)
            if parsed is not None:
                messages.append(parsed)
        return messages

    def _length(self) -> int | None:
        kind = self._buffer[0]
        if kind in _FIXED:
            return _FIXED[kind]
        if kind == 2:
            return 4 + 4 * int.from_bytes(self._buffer[2:4], "big") if len(self._buffer) >= 4 else None
        if kind == 6:
            if len(self._buffer) < 8:
                return None
            size = abs(int.from_bytes(self._buffer[4:8], "big", signed=True))
            if size > _MAX_CUT_TEXT:
                raise ProtocolError("clipboard message too large")
            return 8 + size
        if kind == 248:
            return 9 + self._buffer[8] if len(self._buffer) >= 9 else None
        if kind == 251:
            return 8 + 16 * self._buffer[6] if len(self._buffer) >= 8 else None
        raise ProtocolError(f"unknown RFB client message type {kind}")


def _parse(message: bytes) -> object | None:
    kind = message[0]
    if kind == 3:
        return UpdateRequest(incremental=message[1] != 0)
    if kind == 4:
        return Key(down=message[1] != 0, keysym=int.from_bytes(message[4:8], "big"))
    if kind == 5:
        x, y = struct.unpack(">HH", message[2:6])
        return Pointer(buttons=message[1], x=x, y=y)
    return None


def server_init(width: int, height: int, name: bytes = b"hermex-screen") -> bytes:
    """ServerInit with the 32-bit true-color format stock Xvnc reports."""
    pixel_format = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
    return struct.pack(">HH", width, height) + pixel_format + struct.pack(">I", len(name)) + name


def _compact_length(size: int) -> bytes:
    out = bytearray()
    for shift in (0, 7):
        byte = size >> shift & 0x7F
        if size >> (shift + 7):
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)
    out.append(size >> 14 & 0xFF)
    return bytes(out)


def jpeg_update(width: int, height: int, jpeg: bytes) -> bytes:
    """One FramebufferUpdate holding the whole picture as a Tight JPEG rectangle."""
    if len(jpeg) >= 1 << 22:
        raise ValueError("a Tight length is 22 bits")
    header = struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, width, height, 7)
    return header + bytes([0x90]) + _compact_length(len(jpeg)) + jpeg


def desktop_size_update(width: int, height: int) -> bytes:
    """Tells the viewer the display's size changed, so its next picture fits."""
    return struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, width, height, -223)
