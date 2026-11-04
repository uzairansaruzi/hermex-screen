"""Who controls the desktop. A Mac or Windows desktop is one screen shared by every bot, so a
takeover holds the lease of every Profile, and `computer_use` reads its own Profile's lease file
before each action, so a file write reaches separate gateway and worker processes.

A new Profile could act in the moment before the loop reaches it, because a missing lease file
means the agent holds control, and a stock host can't prevent that. The hold survives a dashboard
restart in this plugin's data directory, so its timer still ends in a hand-back. Only leases this
plugin still holds are ever released.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

IDLE_LIMIT = 15 * 60
LOOP_INTERVAL = 2.0
_REASON = "hermex-screen"


class ControlLost(RuntimeError):
    """This viewer no longer holds control, so nothing more of its input may run."""


def viewer_hash(viewer_id: Optional[str]) -> Optional[str]:
    """The form the stock lease shows of an id: its SHA-256's first 12 hex characters."""
    return hashlib.sha256(viewer_id.encode()).hexdigest()[:12] if viewer_id else None


class Control:
    """`lease` is `tools.bot_desktop.lease`, `profile_paths` lists every Profile's home, and `path`
    is where the hold is saved. Private Hermes APIs, so `/doctor` checks them and control is
    refused if they change."""

    def __init__(self, lease: Any, profile_paths: Callable[[], list[str]], path: Path,
                 clock: Callable[[], float] = time.time) -> None:
        self._lease = lease
        self._profile_paths = profile_paths
        self._path = path
        self._clock = clock
        self._lock = threading.RLock()
        self._holder: Optional[str] = None
        self._last_input = 0.0
        # The Profiles this hold has reached. One of them showing no hold means it ended elsewhere.
        self._applied: set[str] = set()
        # A holder being replaced, whose leases may remain if the replacement stops partway.
        self._replacing: Optional[str] = None
        self._listeners: list[Callable[[Optional[str], bool], None]] = []
        self._thread: Optional[threading.Thread] = None
        self._load()

    @property
    def holder(self) -> Optional[str]:
        return self._holder

    def may_send_input(self, viewer_id: str) -> bool:
        return self._holder == viewer_id

    @contextmanager
    def while_holding(self, viewer_id: str) -> Iterator[None]:
        """Runs one input operation only while `viewer_id` holds, and makes a hand-back wait for it,
        so no key lands after control has returned to the bot."""
        with self._lock:
            if self._holder != viewer_id:
                raise ControlLost()
            yield

    def subscribe(self, listener: Callable[[Optional[str], bool], None]) -> Callable[[], None]:
        """`listener(holder, expired)` hears each change of holder, from any thread."""
        with self._lock:
            self._listeners.append(listener)

        def remove() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)
        return remove

    def acquire(self, viewer_id: str) -> None:
        with self._lock:
            previous = self._holder
            paths = self._profile_paths()
            # Saved first and listing every Profile, so a crash partway through can't strand a lease.
            self._holder, self._applied = viewer_id, set(paths)
            self._replacing = previous if previous != viewer_id else None
            self._last_input = self._clock()
            self._save()
            try:
                for path in paths:
                    self._lease.acquire(viewer_id, profile_key=path, reason=_REASON)
            except Exception:
                self._release_all(viewer_id)
                raise
            self._replacing = None
            self._save()
            self._ensure_loop()
            if previous != viewer_id:
                self._notify(viewer_id, expired=False)

    def release(self, viewer_id: Optional[str]) -> None:
        """Releases `viewer_id`'s hold, which leaves anyone else's in place. None clears any holder,
        which also covers a takeover whose plugin was removed and reinstalled."""
        with self._lock:
            if viewer_id is not None and viewer_id != self._holder:
                return
            self._release_all(viewer_id)
            self._notify(None, expired=False)

    def _release_all(self, viewer_id: Optional[str]) -> None:
        for path in self._profile_paths():
            self._lease.release(viewer_id, profile_key=path)
            if self._replacing is not None:
                self._lease.release(self._replacing, profile_key=path)
        self._holder = None
        self._applied = set()
        self._replacing = None
        self._save()

    def note_input(self) -> None:
        self._last_input = self._clock()

    def public_lease(self, profile_key: Optional[str] = None) -> dict[str, Any]:
        """The lease as a stock host reports it, so the app reads one shape."""
        return self._lease.public_view(self._lease.get(profile_key))

    def tick(self) -> None:
        """Reaches a new Profile, and hands back at the idle limit or once a lease shows it ended."""
        with self._lock:
            if self._holder is None:
                return
            paths = self._profile_paths()
            if self._clock() - self._last_input >= IDLE_LIMIT or any(
                    self._lease.get(path).viewer_id != self._holder for path in paths if path in self._applied):
                # Past the limit, or handed back or taken on some Profile by another client. Naming
                # the id leaves anyone else's lease alone.
                self._release_all(self._holder)
                self._notify(None, expired=True)
            else:
                for path in paths:
                    if path not in self._applied:
                        self._applied.add(path)
                        self._save()
                        self._lease.acquire(self._holder, profile_key=path, reason=_REASON)
                self._save()

    def _ensure_loop(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="hermex-screen-control")
        self._thread.start()

    def _loop(self) -> None:
        while self._holder is not None:
            time.sleep(LOOP_INTERVAL)
            try:
                self.tick()
            except Exception:
                logger.exception("hermex-screen: control loop")

    def _notify(self, holder: Optional[str], expired: bool) -> None:
        """Called inside the lock, so listeners hear changes in the order they happened. They only
        schedule work on their own loop."""
        for listener in list(self._listeners):
            listener(holder, expired)

    def _load(self) -> None:
        try:
            saved = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return
        holder = saved.get("holder")
        if not isinstance(holder, str) or not holder:
            return
        self._holder = holder
        self._last_input = float(saved.get("last_input") or 0.0)
        # What it reached, as saved. A Profile that shows someone else now was taken after this hold,
        # so the next tick ends the hold rather than taking that Profile back.
        self._applied = {str(path) for path in saved.get("applied") or []}
        self._replacing = saved.get("replacing") if isinstance(saved.get("replacing"), str) else None
        # The timer carries on from where it was, and a hold the host no longer shows just ends.
        self._ensure_loop()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._holder is None:
            self._path.unlink(missing_ok=True)
            return
        temporary = self._path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"holder": self._holder, "last_input": self._last_input, "applied": sorted(self._applied),
                       "replacing": self._replacing}, handle)
        os.replace(temporary, self._path)
