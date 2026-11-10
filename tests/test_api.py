import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

from fake_driver import FakeDriverChild

_spec = importlib.util.spec_from_file_location(
    "hermex_screen_plugin_api", Path(__file__).resolve().parent.parent / "dashboard" / "plugin_api.py")
plugin_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin_api)


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.child = FakeDriverChild()
        driver = self.child.driver()
        self.addCleanup(self.child.cleanup)
        self.addCleanup(driver.stop)
        control = mock.Mock()
        control.public_lease.return_value = None
        for name, value in {"_driver": driver, "_control_problem": lambda: None, "_private_api_problem": lambda: None,
                            "_isolation_problem": lambda: None, "platform_refusal": lambda: None,
                            "_control": lambda: control, "locked": lambda: False,
                            "lock_accepts_input": lambda: False}.items():
            patcher = mock.patch.object(plugin_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_failed_permission_check_is_a_driver_problem_and_check_again_recovers(self):
        self.child.break_next_child()
        status = json.loads(plugin_api.get_status().body)
        self.assertIsNone(status["permissions"])
        self.assertIn("couldn't check its permissions: stuck", status["driver"]["problem"])
        status = json.loads(plugin_api.get_status().body)
        self.assertIsNone(status["driver"]["problem"])
        self.assertEqual(status["permissions"], {"accessibility": True, "screen_recording": True})

    def test_doctor_says_it_couldnt_check_rather_than_not_granted(self):
        self.child.break_next_child()
        checks = {check["name"]: check for check in json.loads(plugin_api.get_doctor().body)["checks"]}
        self.assertFalse(checks["accessibility"]["ok"])
        self.assertIn("couldn't check", checks["accessibility"]["detail"])


if __name__ == "__main__":
    unittest.main()
