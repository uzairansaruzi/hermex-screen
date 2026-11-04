import unittest

from hermex_screen.rfb import (
    ClientParser, HandshakeReply, Key, Pointer, ProtocolError, UpdateRequest, jpeg_update, server_init,
)


class ParserTests(unittest.TestCase):
    def stream(self) -> bytes:
        return (b"RFB 003.008\n" + bytes([1, 1])            # the handshake
                + bytes(20)                                  # SetPixelFormat
                + bytes([2, 0, 0, 1, 0, 0, 0, 7])            # SetEncodings
                + bytes([3, 1, 0, 0, 0, 0, 7, 128, 4, 56])   # FramebufferUpdateRequest
                + bytes([4, 1, 0, 0, 0, 0, 0, 0x61])         # KeyEvent
                + bytes([5, 1, 0, 10, 0, 20]))               # PointerEvent

    def test_messages_come_out_the_same_at_any_chunk_boundary(self):
        expected = [HandshakeReply("security"), HandshakeReply("security_result"), HandshakeReply("server_init"),
                    UpdateRequest(incremental=True), Key(down=True, keysym=0x61), Pointer(buttons=1, x=10, y=20)]
        data = self.stream()
        for size in (len(data), 7, 1):
            parser = ClientParser()
            out = []
            for start in range(0, len(data), size):
                out += parser.feed(data[start:start + size])
            self.assertEqual(out, expected)

    def test_each_handshake_reply_waits_for_the_clients_own_bytes(self):
        parser = ClientParser()
        self.assertEqual(parser.feed(b"RFB 003.0"), [])
        self.assertEqual(parser.feed(b"08\n"), [HandshakeReply("security")])
        self.assertEqual(parser.feed(bytes([1])), [HandshakeReply("security_result")])
        self.assertEqual(parser.feed(bytes([1])), [HandshakeReply("server_init")])

    def test_an_unknown_message_closes_rather_than_hiding_input_behind_it(self):
        parser = ClientParser()
        parser.feed(bytes(14))
        with self.assertRaises(ProtocolError):
            parser.feed(bytes([99]))


class ServerTests(unittest.TestCase):
    def test_a_frame_is_one_tight_jpeg_rectangle_with_a_compact_length(self):
        jpeg = bytes(300)
        update = jpeg_update(1920, 1080, jpeg)
        self.assertEqual(update[:4], bytes([0, 0, 0, 1]))
        self.assertEqual(int.from_bytes(update[4 + 8:4 + 12], "big", signed=True), 7)
        self.assertEqual(update[16], 0x90)
        self.assertEqual(update[17:19], bytes([300 & 0x7F | 0x80, 300 >> 7]))
        self.assertEqual(update[19:], jpeg)

    def test_server_init_names_its_size_and_length(self):
        init = server_init(1920, 1080, b"x")
        self.assertEqual(init[:4], bytes([7, 128, 4, 56]))
        self.assertEqual(init[20:24], bytes([0, 0, 0, 1]))
        self.assertEqual(init[24:], b"x")


if __name__ == "__main__":
    unittest.main()
