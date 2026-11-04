import unittest

from hermex_screen.input import InputMapper
from hermex_screen.keys import Paste, Press, action_for
from hermex_screen.rfb import Key, Pointer


class FakeDriver:
    def __init__(self, windows=()):
        self.calls = []
        self.windows = list(windows)

    def call(self, name, **arguments):
        self.calls.append((name, arguments))
        if name == "list_windows":
            return {"structuredContent": {"windows": self.windows}}
        return {}

    def names(self):
        return [name for name, _ in self.calls]


def window(pid, window_id, z, x=0, y=0, w=500, h=500):
    return {"pid": pid, "window_id": window_id, "z_index": z, "layer": 0, "is_on_screen": True,
            "bounds": {"x": x, "y": y, "width": w, "height": h}}


class KeyTests(unittest.TestCase):
    def test_each_kind_of_character_finds_its_way(self):
        self.assertEqual(action_for(0x61), Press("a"))
        self.assertEqual(action_for(0x41), Press("a", shift=True))
        self.assertEqual(action_for(0x31), Press("1"))
        self.assertEqual(action_for(0x20), Press("space"))
        self.assertEqual(action_for(0xFF0D), Press("return"))
        self.assertEqual(action_for(0x21), Paste("!"))
        self.assertEqual(action_for(0xE9), Paste("é"))
        self.assertEqual(action_for(0x0100_20AC), Paste("€"))
        self.assertIsNone(action_for(0x07))

    def test_a_held_modifier_goes_with_the_next_key_and_key_up_sends_nothing(self):
        driver = FakeDriver()
        mapper = InputMapper(driver, "darwin")
        mapper.apply(Key(True, 0xFFEB))
        mapper.apply(Key(True, 0x63))
        mapper.apply(Key(False, 0x63))
        mapper.apply(Key(False, 0xFFEB))
        mapper.apply(Key(True, 0x41))
        self.assertEqual(driver.calls, [
            ("press_key", {"key": "c", "modifiers": ["cmd"], "scope": "desktop"}),
            ("press_key", {"key": "a", "modifiers": ["shift"], "scope": "desktop"}),
        ])

    def test_a_character_with_no_key_pastes_and_the_clipboard_is_cleared_after(self):
        driver = FakeDriver()
        mapper = InputMapper(driver, "win32")
        mapper.apply(Key(True, 0x21))
        self.assertEqual(driver.calls, [("clipboard_write", {"text": "!"}),
                                        ("hotkey", {"keys": ["ctrl", "v"], "scope": "desktop"})])
        mapper.clear_clipboard()
        self.assertEqual(driver.calls[-1], ("clipboard_write", {"text": ""}))
        mapper.clear_clipboard()
        self.assertEqual(len(driver.calls), 3)


class PointerTests(unittest.TestCase):
    def test_a_tap_brings_the_topmost_window_under_it_forward_before_clicking(self):
        driver = FakeDriver([window(1, 10, z=1), window(2, 20, z=5), window(3, 30, z=9, x=900)])
        mapper = InputMapper(driver, "darwin")
        mapper.scale = 2.0
        mapper.point_scale = 2.0
        mapper.apply(Pointer(1, 100, 100))
        mapper.apply(Pointer(0, 100, 100))
        self.assertEqual(driver.calls, [
            ("list_windows", {"on_screen_only": True}),
            ("bring_to_front", {"pid": 2, "window_id": 20}),
            ("click", {"x": 200.0, "y": 200.0, "button": "left", "scope": "desktop"}),
        ])

    def test_a_press_that_moved_is_a_drag_and_right_button_stays_right(self):
        driver = FakeDriver()
        mapper = InputMapper(driver, "darwin")
        mapper.apply(Pointer(1, 10, 10))
        mapper.apply(Pointer(1, 50, 10))
        mapper.apply(Pointer(0, 50, 10))
        name, arguments = driver.calls[-1]
        self.assertEqual(name, "drag")
        self.assertEqual((arguments["from_x"], arguments["to_x"]), (10, 50))

        mapper.apply(Pointer(4, 5, 5))
        mapper.apply(Pointer(0, 5, 5))
        self.assertEqual(driver.calls[-1][1]["button"], "right")

    def test_wheel_buttons_scroll_where_the_pointer_is(self):
        driver = FakeDriver()
        mapper = InputMapper(driver, "darwin")
        mapper.apply(Pointer(16, 7, 8))
        mapper.apply(Pointer(0, 7, 8))
        self.assertEqual(driver.calls, [("scroll", {"direction": "down", "amount": 1, "x": 7, "y": 8, "scope": "desktop"})])


if __name__ == "__main__":
    unittest.main()
