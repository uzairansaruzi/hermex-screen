# hermex-screen

Watch and control this computer's real desktop from [Hermex](https://github.com/uzairansaruzi/hermex), on hosts whose Hermes has no stock Bot Screen. That's Mac today, and Windows for testers.

Anyone with your server's login can see and control everything on this computer while it's installed. It's off until you install it, and Hermex asks first.

Frames and input go directly between this host and your phone, over the dashboard's own address. There's no relay, and nothing is stored but the takeover's own timer.

## Requirements

- Hermes 0.21.2 or newer, with `plugins.isolation: in_process`, the default.
- The `cua-driver` Hermes installs for `computer_use`: `hermes computer-use install`, with Accessibility and Screen Recording granted to CuaDriver.
- A logged-in session on the host. A locked screen shows only the lock screen.

## Check it

`GET /api/plugins/hermex-screen/doctor` reports pass or fail for each requirement, and sends no input.

## Control

A takeover holds every Profile's lease, so `computer_use` pauses on every bot on this host and answers `human_has_control`. It ends on Hand Back, or after 15 minutes with no input from the phone, connected or not.

A window that didn't exist before can act in the moment before the 2-second loop reaches a brand-new Profile. A Windows host has no cross-process lock on the lease file. Both are accepted for a short takeover.

These rely on private Hermes APIs, so control is refused if they change, and `/doctor` says so.

## Windows

Off until the host sets `HERMEX_SCREEN_WINDOWS=1` for the dashboard. Untested beyond that so far.

## Tests

`PYTHONPATH=. python3 -m unittest discover -s tests`
