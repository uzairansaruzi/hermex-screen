import unittest

from fake_driver import FakeDriverChild
from hermex_screen.driver import DriverUnavailable


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.child = FakeDriverChild()
        self.driver = self.child.driver()
        self.addCleanup(self.child.cleanup)
        self.addCleanup(self.driver.stop)

    def test_a_child_that_refuses_a_capture_is_replaced_by_the_next_one(self):
        """The first stream closes 4001; the next one gets frames from a fresh child."""
        self.child.break_next_child()
        with self.assertRaisesRegex(DriverUnavailable, "stuck"):
            self.driver.capture()
        self.assertEqual(self.driver.capture().width, 1)

    def test_a_refused_input_keeps_the_child(self):
        """An action can be refused for its own reasons, which say nothing about the child."""
        self.child.break_next_child()
        with self.assertRaises(DriverUnavailable):
            self.driver.call("click", x=1, y=1)
        with self.assertRaises(DriverUnavailable):
            self.driver.capture()


if __name__ == "__main__":
    unittest.main()
