"""
Incoming OSC listener for transport control.

Supported addresses:
  /play    — start or resume playback
  /pause   — pause playback
  /stop    — stop and rewind to start TC
  /toggle  — toggle play/pause
  /track <int|str> — switch track by 0-based index or name (no-op on miss)
"""

from __future__ import annotations

import threading
from typing import Callable

from pythonosc import dispatcher as _dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer


class OSCServer:
    def __init__(
        self,
        port: int,
        on_play: Callable,
        on_pause: Callable,
        on_stop: Callable,
        on_toggle: Callable,
        on_track: Callable[[int | str], None],
    ) -> None:
        d = _dispatcher.Dispatcher()
        d.map("/play", lambda addr, *args: on_play())
        d.map("/pause", lambda addr, *args: on_pause())
        d.map("/stop", lambda addr, *args: on_stop())
        d.map("/toggle", lambda addr, *args: on_toggle())
        d.map("/track", self._handle_track)
        self._on_track = on_track
        self._server = ThreadingOSCUDPServer(("0.0.0.0", port), d)
        self._thread: threading.Thread | None = None

    def _handle_track(self, address: str, *args) -> None:
        if not args:
            return
        self._on_track(args[0])

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="osc-listener"
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
