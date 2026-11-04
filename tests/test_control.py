import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermex_screen.control import IDLE_LIMIT, Control, ControlLost, viewer_hash
from hermex_screen.driver import CuaDriver, DriverUnavailable, contract_problem


@dataclass
class FakeLease:
    holder: str = "agent"
    viewer_id: Optional[str] = None


class FakeLeases:
    """A stock lease: an id only releases itself, and None clears any holder."""

    def __init__(self):
        self.files = {}

    def get(self, profile_key=None):
        return self.files.setdefault(profile_key, FakeLease())

    def acquire(self, viewer_id, *, profile_key=None, reason=""):
        self.files[profile_key] = FakeLease("human", viewer_id)

    def release(self, viewer_id=None, *, profile_key=None, unless_human=False):
        lease = self.get(profile_key)
        if viewer_id is None or lease.viewer_id == viewer_id:
            self.files[profile_key] = FakeLease()

    def public_view(self, lease):
        return {"holder": lease.holder, "viewer_hash": viewer_hash(lease.viewer_id)}


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "hold.json"
        self.leases = FakeLeases()
        self.profiles = ["/a", "/b"]
        self.now = 1000.0

    def control(self):
        control = Control(self.leases, lambda: list(self.profiles), self.path, clock=lambda: self.now)
        self.addCleanup(lambda: setattr(control, "_holder", None))
        return control

    def holders(self):
        return {path: self.leases.get(path).holder for path in self.profiles}

    def test_take_over_holds_every_profile_and_hand_back_frees_them(self):
        control = self.control()
        control.acquire("v1")
        self.assertEqual(self.holders(), {"/a": "human", "/b": "human"})
        control.release("v1")
        self.assertEqual(self.holders(), {"/a": "agent", "/b": "agent"})
        self.assertFalse(self.path.exists())

    def test_a_profile_created_during_a_takeover_is_held_on_the_next_tick(self):
        control = self.control()
        control.acquire("v1")
        self.profiles.append("/new")
        control.tick()
        self.assertEqual(self.leases.get("/new").viewer_id, "v1")

    def test_a_lease_cleared_by_another_client_ends_the_hold_everywhere(self):
        control = self.control()
        control.acquire("v1")
        self.leases.release(None, profile_key="/a")
        control.tick()
        self.assertIsNone(control.holder)
        self.assertEqual(self.holders(), {"/a": "agent", "/b": "agent"})

    def test_an_old_id_cant_release_a_newer_takeover(self):
        control = self.control()
        control.acquire("v1")
        control.acquire("v2")
        control.release("v1")
        self.assertEqual(self.leases.get("/a").viewer_id, "v2")
        self.assertFalse(control.may_send_input("v1"))

    def test_force_clears_any_holder_including_one_with_no_saved_hold(self):
        self.leases.acquire("left-by-a-removed-plugin", profile_key="/a")
        self.control().release(None)
        self.assertEqual(self.leases.get("/a").holder, "agent")

    def test_idle_hands_back_at_the_limit_and_input_puts_it_off(self):
        control = self.control()
        heard = []
        control.subscribe(lambda holder, expired: heard.append((holder, expired)))
        control.acquire("v1")
        self.now += IDLE_LIMIT - 1
        control.note_input()
        self.now += IDLE_LIMIT - 1
        control.tick()
        self.assertEqual(self.holders()["/a"], "human")
        self.now += 2
        control.tick()
        self.assertEqual(self.holders()["/a"], "agent")
        self.assertEqual(heard[-1], (None, True))

    def test_a_restart_mid_takeover_still_ends_in_a_hand_back(self):
        self.control().acquire("v1")
        self.now += IDLE_LIMIT + 1
        reborn = self.control()
        self.assertEqual(reborn.holder, "v1")
        reborn.tick()
        self.assertEqual(self.holders(), {"/a": "agent", "/b": "agent"})
        self.assertFalse(self.path.exists())


    def test_a_restart_never_takes_back_a_lease_someone_else_took_meanwhile(self):
        self.control().acquire("v1")
        self.leases.acquire("someone-else", profile_key="/a")
        reborn = self.control()
        reborn.tick()
        self.assertIsNone(reborn.holder)
        self.assertEqual(self.leases.get("/a").viewer_id, "someone-else")
        self.assertEqual(self.holders()["/b"], "agent")

    def test_a_failure_partway_through_a_takeover_strands_no_lease(self):
        control = self.control()
        original = self.leases.acquire

        def fail_second(viewer_id, *, profile_key=None, reason=""):
            if profile_key == "/b":
                raise OSError("disk full")
            original(viewer_id, profile_key=profile_key, reason=reason)
        self.leases.acquire = fail_second
        with self.assertRaises(OSError):
            control.acquire("v1")
        self.assertEqual(self.holders(), {"/a": "agent", "/b": "agent"})
        self.assertIsNone(control.holder)

    def test_a_replacement_failing_partway_strands_neither_holder(self):
        control = self.control()
        control.acquire("v1")
        original = self.leases.acquire

        def fail_second(viewer_id, *, profile_key=None, reason=""):
            if profile_key == "/b":
                raise OSError("disk full")
            original(viewer_id, profile_key=profile_key, reason=reason)
        self.leases.acquire = fail_second
        with self.assertRaises(OSError):
            control.acquire("v2")
        self.assertEqual(self.holders(), {"/a": "agent", "/b": "agent"})

    def test_input_after_control_is_lost_is_refused(self):
        control = self.control()
        control.acquire("v1")
        with control.while_holding("v1"):
            pass
        control.release("v1")
        with self.assertRaises(ControlLost):
            with control.while_holding("v1"):
                self.fail("the operation must not run")


class DriverContractTests(unittest.TestCase):
    def test_a_driver_that_cant_do_the_job_does_none_of_it(self):
        driver = CuaDriver(command="unused", env={})
        driver.tools = lambda: []
        driver.problem = "this driver has no click tool"
        called = []
        driver._request = lambda *args: called.append(args) or {}
        with self.assertRaises(DriverUnavailable):
            driver.call("click", x=1, y=1)
        self.assertEqual(called, [])


class ContractTests(unittest.TestCase):
    def test_an_older_driver_without_scope_is_explained(self):
        tools = [{"name": "click", "inputSchema": {"properties": {"capture_scope": {}}}}]
        self.assertIn("get_desktop_state", contract_problem(tools))
        everything = [{"name": name, "inputSchema": {"properties": {key: {} for key in (
            "scope", "button", "modifiers", "text", "on_screen_only", "pid", "window_id")}}}
            for name in ("get_desktop_state", "click", "drag", "scroll", "press_key", "hotkey",
                         "clipboard_write", "list_windows", "bring_to_front", "check_permissions")]
        self.assertIsNone(contract_problem(everything))
        for tool in everything:
            if tool["name"] == "click":
                del tool["inputSchema"]["properties"]["scope"]
        self.assertIn("click", contract_problem(everything))


if __name__ == "__main__":
    unittest.main()
