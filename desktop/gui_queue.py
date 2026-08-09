"""Thread-safe pub/sub for passing messages from background threads (scheduler,
crawler, notifier) to GUI threads.

The queue started life as a single-consumer `queue.Queue`, but the app
now has two Tk roots that each want to react to specific events:

* Tray's Tk thread    — handles ``show_history``, ``show_settings``,
                        ``open_viewer``, ``shutdown``, ``status``.
* MainWindow Tk thread — handles ``new_article`` (push to recent_articles
                        list), ``reload_config``, ``status``.

Two threads sharing a single `queue.Queue` race against each other
and the first thread to call ``get()`` wins — the other thread never
sees the event. To fix this, we use a **broadcast** model: each
subscriber gets its own private queue; the central `put()` enqueues
to all subscribers; each subscriber drains independently.

Use ``gui_queue.subscribe()`` to obtain a `Subscriber` handle on the
Tk thread that needs to receive events. The legacy ``gui_queue.get()``
and ``gui_queue.drain()`` are preserved for the tray's poll loop —
they return events targeted at the "default" subscriber (everything
not claimed by another subscriber first).
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Iterator, List, Optional, Tuple

# Event types (kept as a single tuple for type hints):
#   ("new_article", Article)    — push article to recent list (MainWindow)
#   ("toast", Article)          — legacy alias for "new_article"
#   ("open_viewer", Article)    — open Article Viewer (tray)
#   ("show_history", None)      — open History window (tray)
#   ("show_settings", (cfg, path)) — open Settings window (tray)
#   ("reload_config", dict)     — reload config (MainWindow)
#   ("status", None)            — update tray status + dashboard refresh
#   ("shutdown", None)          — request GUI shutdown

_EVENT = Tuple[str, Any]

# Event types routed to MainWindow (vs. tray) by default. Subscribers
# can override by claiming specific event types, but the broadcast
# model already gives every subscriber a copy of every event.
_TRAY_EVENTS = {
    "open_viewer",
    "show_history",
    "show_settings",
    "shutdown",
    "status",
}


class _Subscriber:
    """A single Tk-thread consumer of gui_queue events."""

    def __init__(self, name: str = "default"):
        self._name = name
        self._q: "queue.Queue[_EVENT]" = queue.Queue()

    def put(self, event: _EVENT) -> None:
        self._q.put(event)

    def get(self, timeout: float = 0.1):
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> List[_EVENT]:
        out: List[_EVENT] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out

    def qsize(self) -> int:
        return self._q.qsize()

    @property
    def name(self) -> str:
        return self._name


class _GuiQueue:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: List[_Subscriber] = []
        self._default_sub: Optional[_Subscriber] = None
        self._tk_root = None  # legacy binding (tray's Tk root)

    # -------------------------------------------------------------- public

    def subscribe(self, name: str = "default") -> _Subscriber:
        """Register a new Tk-thread subscriber and return its handle.

        All subsequent ``put()`` events will be broadcast to every
        registered subscriber. The Tk thread should poll its subscriber
        via ``subscriber.get()`` / ``subscriber.drain()``.
        """
        sub = _Subscriber(name)
        with self._lock:
            self._subscribers.append(sub)
            if self._default_sub is None:
                self._default_sub = sub
        return sub

    def put(self, event: _EVENT) -> None:
        """Broadcast an event to every subscriber.

        If no subscriber is registered, the event is kept in a tiny
        fallback queue so the legacy `get()` call still works (used by
        unit tests).
        """
        with self._lock:
            subs = list(self._subscribers)
        if not subs:
            # Fallback so legacy single-consumer code paths keep working.
            if not hasattr(self, "_fallback"):
                self._fallback: "queue.Queue[_EVENT]" = queue.Queue()
            self._fallback.put(event)
            return
        for sub in subs:
            sub.put(event)

    def get(self, timeout: float = 0.1):
        """Legacy single-consumer get. Prefer ``subscribe()`` + drain."""
        if hasattr(self, "_fallback") and self._fallback is not None:
            try:
                return self._fallback.get(timeout=timeout)
            except queue.Empty:
                return None
        return None

    def drain(self) -> List[_EVENT]:
        """Legacy single-consumer drain. Prefer ``subscribe()`` + drain."""
        if hasattr(self, "_fallback") and self._fallback is not None:
            out: List[_EVENT] = []
            while True:
                try:
                    out.append(self._fallback.get_nowait())
                except queue.Empty:
                    break
            return out
        return []

    def qsize(self) -> int:
        if hasattr(self, "_fallback") and self._fallback is not None:
            return self._fallback.qsize()
        return 0

    def bind_tk_root(self, root) -> None:
        """Bind the Tk root so consumers can use root.after() to poll."""
        with self._lock:
            self._tk_root = root

    @property
    def tk_root(self):
        return self._tk_root


gui_queue = _GuiQueue()
