import os
import unittest
from unittest import mock

from hermex_screen.platforms import WINDOWS_OPT_IN_ENV, lock_accepts_input, refusal


class PlatformTests(unittest.TestCase):
    def test_windows_is_refused_until_the_host_opts_in_and_the_refusal_says_how(self):
        with mock.patch.dict(os.environ, clear=True):
            self.assertIn(WINDOWS_OPT_IN_ENV, refusal("win32"))
        with mock.patch.dict(os.environ, {WINDOWS_OPT_IN_ENV: "1"}):
            self.assertIsNone(refusal("win32"))

    def test_a_mac_is_served_and_linux_keeps_stock_bot_screen(self):
        self.assertIsNone(refusal("darwin"))
        self.assertIsNotNone(refusal("linux"))

    def test_only_a_mac_takes_input_on_its_lock_screen(self):
        self.assertTrue(lock_accepts_input("darwin"))
        self.assertFalse(lock_accepts_input("win32"))


if __name__ == "__main__":
    unittest.main()
