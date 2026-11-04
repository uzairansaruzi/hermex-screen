# Testing hermex-screen on Windows

Nobody has run this on Windows yet, so expect basic failures. Thanks for trying it. Report what you find, with the output from step 3, in a [Hermex issue](https://github.com/uzairansaruzi/hermex/issues).

Anyone with your server's login can see and control everything on this PC while the plugin is on. Test on a PC where that's fine, and stay nearby.

1. In Hermex: Settings → your server → turn on **Bot Screen (beta)**, then open a bot's **Screen** and tap **Install Plugin**.
2. On the PC, set `HERMEX_SCREEN_WINDOWS=1` where you start `hermes dashboard`, and run it in your logged-in session, not as a service. Restart it.
3. Run the self-check, signed in to the dashboard: `GET /api/plugins/hermex-screen/doctor`. Every check should pass, and it sends no input. Fix anything it reports first.
4. In Hermex, open **Screen** again. You should see your desktop, with the subtitle "This computer's real desktop".
5. Open Notepad as a throwaway window. Tap **Take Over**, tap inside Notepad, and type `Hello 42!é`. Check all of it landed, in Notepad.
6. Switch apps on the phone for a minute, then come back. You should still have control, with no prompt.
7. Tap **Hand Back**. A bot should be able to use `computer_use` again.
8. Lock the PC. Hermex should say the screen is locked, and not offer **Take Over**.

To remove it, open Screen's **…** menu and tap **Remove from This Host**.
