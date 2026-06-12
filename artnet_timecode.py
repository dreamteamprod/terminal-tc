#!/usr/bin/env python3
"""
ArtNet Timecode Player
======================
Sends SMPTE timecode over Art-Net (ArtTimeCode) and optionally
plays an audio file in sync.

Cross-platform: Linux, macOS, Windows
Dependencies: rich, timecode, sounddevice, soundfile, numpy

Usage examples:
  python artnet_timecode.py
  python artnet_timecode.py --ip 2.255.255.255 --fps 25
  python artnet_timecode.py --ip 192.168.1.255 --fps 30 \\
      --start-hours 1 --start-minutes 0 --start-seconds 0 --start-frames 0
  python artnet_timecode.py --ip 192.168.1.255 --fps 25 --audio show.wav
"""

import argparse
import socket
import struct
import sys
import threading
import time
import os
from enum import IntEnum
from typing import Optional

# ── Third-party ───────────────────────────────────────────────────────────────
try:
    from rich.console import Console  # kept for non-interactive fallback only
except ImportError:
    Console = None  # type: ignore

try:
    from timecode import Timecode as _LibTimecode
except ImportError:
    sys.exit("Missing dependency: pip install timecode")

try:
    import sounddevice as sd
    import soundfile as sf
    import numpy as np
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False
except OSError:
    AUDIO_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────────
ARTNET_PORT = 6454
ARTNET_ID   = b"Art-Net\x00"
OP_TIMECODE = 0x9700          # ArtTimeCode opcode

SUPPORTED_FPS = [24, 25, 29.97, 30]

FPS_TYPE_MAP = {             # Art-Net type field values
    24:    0,
    25:    1,
    29.97: 2,
    30:    3,
}

FPS_LABEL = {
    24:    "24 fps  (Film / DCI)",
    25:    "25 fps  (EBU / PAL)",
    29.97: "29.97 fps  (Drop Frame / NTSC)",
    30:    "30 fps  (SMPTE / HD)",
}


# ── Art-Net packet builder ─────────────────────────────────────────────────────
def build_artimecode(hours: int, minutes: int, seconds: int,
                     frames: int, fps_type: int) -> bytes:
    """
    Build an ArtTimeCode UDP datagram.

    Art-Net Spec §ArtTimeCode layout:
      ID        8 bytes   "Art-Net\\0"
      OpCode    2 bytes   0x9700  (LE)
      ProtVer   2 bytes   14      (BE)
      Filler    2 bytes
      Frames    1 byte
      Seconds   1 byte
      Minutes   1 byte
      Hours     1 byte
      Type      1 byte    0=Film 1=EBU 2=DropFrame 3=SMPTE
      Filler    1 byte
    """
    return (
        ARTNET_ID
        + struct.pack("<H", OP_TIMECODE)
        + struct.pack(">H", 14)
        + b"\x00\x00"
        + struct.pack("BBBBBB",
                      frames, seconds, minutes, hours,
                      fps_type, 0)
    )


# ── Timecode helpers (wrapping the `timecode` library) ────────────────────────
# The `timecode` library uses a 1-indexed `frames` constructor argument:
#   Timecode(fps, frames=1)  →  00:00:00:00
#   Timecode(fps, frames=2)  →  00:00:00:01
# Its `frame_number` property is 0-indexed (00:00:00:00 → frame_number=0).
# We keep these details internal so the rest of the code stays clean.

# fps argument the library accepts as a string
_FPS_STR = {
    24:    "24",
    25:    "25",
    29.97: "29.97",
    30:    "30",
}


def make_tc(fps: float, hours: int, minutes: int,
            seconds: int, frames: int) -> _LibTimecode:
    """Construct a Timecode from H/M/S/F components."""
    sep = ";" if fps == 29.97 else ":"
    tc_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{frames:02d}"
    return _LibTimecode(_FPS_STR[fps], tc_str)


def tc_from_frame_number(fps: float, frame_number: int) -> _LibTimecode:
    """Construct a Timecode from a 0-based absolute frame number."""
    return _LibTimecode(_FPS_STR[fps], frames=frame_number + 1)


def load_markers(path: str, fps: float) -> list:
    """Load cue markers from a CSV file (columns: #, Name, Start TC).

    Returns a list of (id, name, tc) tuples sorted by ascending timecode.
    The header row (first field == '#') is skipped. Malformed rows are ignored.
    """
    import csv
    markers = []
    try:
        with open(path, newline='', encoding='utf-8') as f:
            for row in csv.reader(f):
                if len(row) < 3 or row[0].strip() == '#':
                    continue
                mid, name, tc_str = row[0].strip(), row[1].strip(), row[2].strip()
                try:
                    tc = _LibTimecode(_FPS_STR[fps], tc_str)
                    markers.append((mid, name, tc))
                except Exception:
                    pass
    except OSError as e:
        sys.exit(f"Cannot open markers file: {e}")
    markers.sort(key=lambda m: m[2].frame_number)
    return markers


# ── State ──────────────────────────────────────────────────────────────────────
class State(IntEnum):
    STOPPED = 0
    PLAYING = 1
    PAUSED  = 2

STATE_STYLE = {
    State.STOPPED: ("red",    "■  STOPPED"),
    State.PLAYING: ("green",  "▶  PLAYING"),
    State.PAUSED:  ("yellow", "⏸  PAUSED"),
}


# ── Player ─────────────────────────────────────────────────────────────────────
class ArtNetTimecodePlayer:
    def __init__(self,
                 start_tc:   _LibTimecode,
                 fps:        float,
                 dest_ip:    str,
                 dest_port:  int,
                 audio_path: Optional[str] = None,
                 broadcast:  bool = False):

        self.start_tc   = start_tc
        self.fps        = fps
        self.dest_ip    = dest_ip
        self.dest_port  = dest_port
        self.audio_path = audio_path
        self.broadcast  = broadcast

        self.fps_type        = FPS_TYPE_MAP.get(fps, 3)
        self._frame_interval = 1.0 / fps
        self.state           = State.STOPPED
        self.packet_count    = 0
        self.error_count     = 0
        self.status_msg      = "Ready"

        # Current displayed timecode
        self._tc_lock = threading.Lock()
        self._tc: _LibTimecode = start_tc

        # Pause/resume state tracking
        self._play_start_wall:  float = 0.0
        self._pause_frame_acc:  int   = 0   # frames already elapsed before pause

        self._stop_event    = threading.Event()
        self._ticker_thread: Optional[threading.Thread] = None

        # UDP socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if broadcast:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        # Audio
        self._audio_data:        Optional[np.ndarray]    = None
        self._audio_samplerate:  int                     = 44100
        self._audio_thread_hdl:  Optional[threading.Thread] = None
        self._audio_pos:         int                     = 0
        self._audio_loaded:      bool                    = False
        self._audio_error:       str                     = ""
        self._audio_channels:    int                     = 2

        if audio_path:
            self._load_audio(audio_path)

    # ── Audio ──────────────────────────────────────────────────────────────────
    def _load_audio(self, path: str) -> None:
        if not AUDIO_AVAILABLE:
            self._audio_error = "sounddevice/soundfile unavailable (install deps)"
            return
        if not os.path.isfile(path):
            self._audio_error = f"File not found: {path}"
            return
        try:
            data, sr = sf.read(path, dtype="float32", always_2d=True)
            self._audio_data       = data
            self._audio_samplerate = sr
            self._audio_channels   = data.shape[1]
            self._audio_loaded     = True
        except Exception as e:
            self._audio_error = f"Load error: {e}"

    def _audio_thread_fn(self, start_pos: int) -> None:
        chunk_size = 1024
        try:
            with sd.OutputStream(
                samplerate=self._audio_samplerate,
                channels=self._audio_channels,
                dtype="float32",
            ) as stream:
                data  = self._audio_data
                total = len(data)
                pos   = start_pos
                while not self._stop_event.is_set() and pos < total:
                    chunk = data[pos:pos + chunk_size]
                    stream.write(chunk)   # blocks in C, GIL released during wait
                    pos += chunk_size
                self._audio_pos = pos
        except Exception as e:
            self._audio_error = f"Audio error: {e}"

    def _start_audio_at(self, audio_frame_offset: int) -> None:
        if not self._audio_loaded or self._audio_data is None:
            return
        self._audio_pos = max(0, audio_frame_offset)
        self._audio_thread_hdl = threading.Thread(
            target=self._audio_thread_fn,
            args=(self._audio_pos,),
            daemon=True,
        )
        self._audio_thread_hdl.start()

    def _stop_audio(self) -> None:
        # _stop_event is set by the caller before this is called.
        # The audio thread checks it after each chunk write (~23ms) and exits.
        # The `with sd.OutputStream` context manager cleans up the stream.
        pass

    # ── Ticker ─────────────────────────────────────────────────────────────────
    def _ticker(self) -> None:
        """
        High-accuracy frame ticker.
        Uses absolute time anchoring to avoid drift accumulation.
        """
        interval       = self._frame_interval
        wall_origin    = self._play_start_wall
        start_fn       = self.start_tc.frame_number + self._pause_frame_acc
        next_tick      = wall_origin
        local_fn       = start_fn

        while not self._stop_event.is_set():
            # Sleep until the next frame deadline
            sleep = next_tick - time.perf_counter()
            if sleep > 0.001:
                time.sleep(sleep - 0.001)
            # Busy-spin the final millisecond for accuracy
            while time.perf_counter() < next_tick:
                pass

            if self._stop_event.is_set():
                break

            tc = tc_from_frame_number(self.fps, local_fn)
            with self._tc_lock:
                self._tc = tc

            try:
                pkt = build_artimecode(
                    tc.hrs, tc.mins, tc.secs, tc.frs,
                    self.fps_type)
                self._sock.sendto(pkt, (self.dest_ip, self.dest_port))
                self.packet_count += 1
            except Exception:
                self.error_count += 1

            local_fn  += 1
            next_tick  = wall_origin + (local_fn - start_fn) * interval

    # ── Transport ──────────────────────────────────────────────────────────────
    def play(self) -> None:
        if self.state == State.PLAYING:
            return
        if self.state == State.STOPPED:
            with self._tc_lock:
                self._tc = self.start_tc
            self._pause_frame_acc = 0

        self._stop_event.clear()
        self._play_start_wall = time.perf_counter()
        self.state = State.PLAYING
        self.status_msg = "Playing"

        # Start audio at matching offset
        if self._audio_loaded:
            audio_offset = round(
                (self._pause_frame_acc / self.fps) * self._audio_samplerate)
            self._start_audio_at(audio_offset)

        self._ticker_thread = threading.Thread(
            target=self._ticker, daemon=True)
        self._ticker_thread.start()

    def pause(self) -> None:
        if self.state != State.PLAYING:
            return
        elapsed_wall = time.perf_counter() - self._play_start_wall
        self._pause_frame_acc += int(elapsed_wall * self.fps)
        self._stop_event.set()
        self.state = State.PAUSED
        self.status_msg = "Paused"
        self._stop_audio()

    def stop(self) -> None:
        if self.state == State.STOPPED:
            return
        self._stop_event.set()
        self.state = State.STOPPED
        self._pause_frame_acc = 0
        self.status_msg = "Stopped"
        self._stop_audio()
        with self._tc_lock:
            self._tc = self.start_tc

    def toggle_play_pause(self) -> None:
        if self.state == State.PLAYING:
            self.pause()
        else:
            self.play()

    def _emit_tc_at_acc(self) -> None:
        """Update _tc and send one Art-Net packet at the current _pause_frame_acc."""
        abs_frame = self.start_tc.frame_number + self._pause_frame_acc
        tc = tc_from_frame_number(self.fps, abs_frame)
        with self._tc_lock:
            self._tc = tc
        try:
            pkt = build_artimecode(tc.hrs, tc.mins, tc.secs, tc.frs, self.fps_type)
            self._sock.sendto(pkt, (self.dest_ip, self.dest_port))
            self.packet_count += 1
        except Exception:
            self.error_count += 1

    def scrub(self, delta_seconds: float) -> None:
        """Shift playback position by delta_seconds (negative = backward). Default step: 5s."""
        delta_frames = round(delta_seconds * self.fps)

        if self.state == State.PLAYING:
            # Capture thread handles before pause() so we can join them.
            # pause() sets _stop_event; joining ensures play()'s _stop_event.clear()
            # doesn't race with old threads that haven't exited yet.
            old_ticker = self._ticker_thread
            old_audio  = self._audio_thread_hdl
            self.pause()
            if old_ticker is not None:
                old_ticker.join(timeout=0.2)
            if old_audio is not None:
                old_audio.join(timeout=0.2)
            self._pause_frame_acc = max(0, self._pause_frame_acc + delta_frames)
            self.play()

        elif self.state == State.PAUSED:
            self._pause_frame_acc = max(0, self._pause_frame_acc + delta_frames)
            self._emit_tc_at_acc()

        else:  # STOPPED → transition to PAUSED at scrubbed position
            self._pause_frame_acc = max(0, delta_frames)
            self.state      = State.PAUSED
            self.status_msg = "Paused"
            self._emit_tc_at_acc()

    def get_tc(self) -> _LibTimecode:
        with self._tc_lock:
            return self._tc

    def audio_duration_str(self) -> str:
        if not self._audio_loaded or self._audio_data is None:
            return ""
        dur = len(self._audio_data) / self._audio_samplerate
        m, s = divmod(int(dur), 60)
        return f"{m}:{s:02d}"

    def seek_to_frame(self, abs_frame: int) -> None:
        """Jump to an absolute frame position (clamps to >= start_tc)."""
        was_playing = (self.state == State.PLAYING)

        if was_playing:
            old_ticker = self._ticker_thread
            old_audio  = self._audio_thread_hdl
            self.pause()
            if old_ticker is not None: old_ticker.join(timeout=0.2)
            if old_audio  is not None: old_audio.join(timeout=0.2)
        elif self.state == State.STOPPED:
            self.state      = State.PAUSED
            self.status_msg = "Paused"

        self._pause_frame_acc = max(0, abs_frame - self.start_tc.frame_number)
        self._emit_tc_at_acc()

        if was_playing:
            self.play()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._ticker_thread is not None:
            self._ticker_thread.join(timeout=0.5)
        if self._audio_thread_hdl is not None:
            self._audio_thread_hdl.join(timeout=1.0)
        try:
            self._sock.close()
        except Exception:
            pass


# ── Flicker-free direct ANSI TUI ──────────────────────────────────────────────
#
# Strategy: enter the alternate screen buffer once, draw the full UI once,
# then on each tick move the cursor to only the lines that change and
# overwrite them in place.  Nothing is ever erased → zero flicker.
#
# ANSI helpers
_ESC = "\033["
def _goto(row: int, col: int) -> str:   return f"{_ESC}{row};{col}H"
def _bold(s: str)             -> str:   return f"\033[1m{s}\033[0m"
def _dim(s: str)              -> str:   return f"\033[2m{s}\033[0m"
def _fg(code: int, s: str)    -> str:   return f"\033[{code}m{s}\033[0m"
def _rev(s: str)              -> str:   return f"\033[7m{s}\033[0m"
def _hide_cursor()            -> str:   return "\033[?25l"
def _show_cursor()            -> str:   return "\033[?25h"
def _alt_screen_on()          -> str:   return "\033[?1049h"
def _alt_screen_off()         -> str:   return "\033[?1049l"
def _clear_screen()           -> str:   return "\033[2J"
def _eol()                    -> str:   return "\033[K"   # erase to end of line

def _top_border(bc: int, w: int) -> str:
    title = "  Art-Net Timecode Player  "
    pad_l = (w - 2 - len(title)) // 2
    pad_r = w - 2 - pad_l - len(title)
    return (
        f"\033[{bc}m╔{'═' * pad_l}"
        f"\033[1;37m{title}"
        f"\033[{bc}m{'═' * pad_r}╗\033[0m"
    )

def _bot_border(bc: int, w: int, status: str) -> str:
    inner = f" {status} " if status else ""
    pad_l = (w - 2 - len(inner)) // 2
    pad_r = w - 2 - pad_l - len(inner)
    return (
        f"\033[{bc}m╚{'═' * pad_l}"
        f"\033[2m{inner}"
        f"\033[{bc}m{'═' * pad_r}╝\033[0m"
    )

# Colours (ANSI 256-colour or basic)
_C = {
    "red":    31, "green": 32, "yellow": 33,
    "cyan":   36, "white": 37, "dim":     2,
}

STATE_ANSI = {
    State.STOPPED: (31, "■  STOPPED"),
    State.PLAYING: (32, "▶  PLAYING"),
    State.PAUSED:  (33, "⏸  PAUSED"),
}

# Fixed row assignments (1-based terminal rows).
# The UI is 20 rows tall; we centre it vertically at draw time.
_ROW_OFFSET  = 0   # set once in DirectTUI.__init__

_REL_BORDER_TOP    = 1
_REL_TITLE         = 1   # drawn in the border
_REL_STATE         = 3
_REL_TC            = 5
_REL_DEST          = 7
_REL_FPS           = 8
_REL_START         = 9
_REL_PACKETS       = 10
_REL_AUDIO         = 11
_REL_CONTROLS      = 13
_REL_BORDER_BOT    = 15
_REL_SUBTITLE      = 15  # drawn in bottom border
UI_HEIGHT          = 16

# Marker panel (populated by _configure_markers_layout when --markers is used)
_N_MARKERS_VISIBLE = 0
_REL_MARKERS_HDR   = 13  # header row — same slot as controls in the base layout
_REL_MARKERS_FIRST = 14  # first marker entry row


def _configure_markers_layout(n_visible: int) -> None:
    """Expand the layout globals to accommodate N visible marker rows.

    Inserts: markers header (1 row) + N entry rows + 1 blank row above controls.
    Everything from _REL_CONTROLS downward shifts by (n_visible + 2).
    """
    global UI_HEIGHT, _REL_CONTROLS, _REL_BORDER_BOT, _REL_SUBTITLE
    global _N_MARKERS_VISIBLE
    _N_MARKERS_VISIBLE = n_visible
    shift = n_visible + 2          # header + entries + trailing blank
    _REL_CONTROLS   = 13 + shift   # was 13
    _REL_BORDER_BOT = 15 + shift   # was 15
    _REL_SUBTITLE   = _REL_BORDER_BOT
    UI_HEIGHT       = 16 + shift   # was 16


class DirectTUI:
    """
    Draws a fixed UI using raw ANSI codes.  Only mutable fields are
    redrawn on each refresh — no screen clears, no flicker.
    """

    def __init__(self, player: ArtNetTimecodePlayer,
                 args: argparse.Namespace,
                 markers: list = []):
        self.player   = player
        self.args     = args
        self._out     = sys.stdout
        self._lock    = threading.Lock()

        # Marker navigation state
        self._markers       = markers
        self._n_vis         = _N_MARKERS_VISIBLE   # set by _configure_markers_layout
        self._marker_cursor = 0
        self._marker_scroll = 0
        self._last_cursor   = None   # forces initial draw in refresh()

        # Detect terminal width; default 80
        try:
            import shutil
            self._cols = shutil.get_terminal_size().columns
        except Exception:
            self._cols = 80

        try:
            import shutil
            rows = shutil.get_terminal_size().lines
        except Exception:
            rows = 24
        # Vertically centre the UI
        global _ROW_OFFSET
        _ROW_OFFSET = max(1, (rows - UI_HEIGHT) // 2)

    # ── Low-level write ────────────────────────────────────────────────────────
    def _w(self, *parts: str) -> None:
        self._out.write("".join(parts))

    def _flush(self) -> None:
        self._out.flush()

    def _row(self, rel: int) -> int:
        return _ROW_OFFSET + rel

    # ── Centre a string within terminal width ──────────────────────────────────
    def _centre(self, text: str, visible_len: int) -> str:
        pad = max(0, (self._cols - visible_len) // 2)
        return " " * pad + text

    # ── Static chrome (drawn once) ─────────────────────────────────────────────
    def draw_static(self) -> None:
        w = self._cols
        c = self.args
        p = self.player

        # Border colour driven by initial state (will be redrawn on state change)
        bc = _C["red"]

        # Static info rows
        dest_str = c.ip
        if c.broadcast or c.ip.endswith(".255"):
            dest_str += _dim("  (broadcast)")
        dest_str += f":{c.port}"

        fps_str   = FPS_LABEL.get(p.fps, f"{p.fps} fps")
        start_str = str(p.start_tc)

        audio_str = self._audio_line()

        if self._markers:
            ctrl = (
                _rev(_bold(" SPACE ")) + _dim("  Play / Pause    ") +
                _rev(_bold("  S  ")) + _dim("  Stop    ") +
                _rev(_bold("  ◀  ▶  ")) + _dim("  Scrub ±5s    ") +
                _rev(_bold("  ↑  ↓  ")) + _dim("  Markers    ") +
                _rev(_bold("  ↩  ")) + _dim("  Jump    ") +
                _rev(_bold("  Q  ")) + _dim("  Quit")
            )
            ctrl_vis = len(
                "  SPACE   Play / Pause      S   Stop      ◀  ▶   Scrub ±5s"
                "      ↑  ↓   Markers      ↩   Jump      Q   Quit")
        else:
            ctrl = (
                _rev(_bold(" SPACE ")) + _dim("  Play / Pause    ") +
                _rev(_bold("  S  ")) + _dim("  Stop    ") +
                _rev(_bold("  ◀  ▶  ")) + _dim("  Scrub ±5s    ") +
                _rev(_bold("  Q  ")) + _dim("  Quit")
            )
            ctrl_vis = len("  SPACE   Play / Pause      S   Stop      ◀  ▶   Scrub ±5s      Q   Quit")

        self._w(_hide_cursor())
        self._w(_goto(self._row(_REL_BORDER_TOP), 1))
        self._w(_top_border(bc, w), _eol(), "\n")

        for rel in range(2, UI_HEIGHT - 1):
            self._w(_goto(self._row(rel), 1))
            self._w(_fg(bc, "║"), " " * (w - 2), _fg(bc, "║"), _eol(), "\n")

        # Static info
        lw = 15   # label column width
        def info_row(rel, label, val):
            self._w(_goto(self._row(rel), 3))
            self._w(_dim(label.rjust(lw)), "  ", val, _eol())

        info_row(_REL_DEST,    "Destination", dest_str)
        info_row(_REL_FPS,     "Frame rate",  fps_str)
        info_row(_REL_START,   "Start TC",    start_str)
        info_row(_REL_AUDIO,   "Audio",       audio_str)

        # Markers header (drawn once; entries are drawn in refresh via _draw_markers)
        if self._markers:
            self._w(_goto(self._row(_REL_MARKERS_HDR), 3))
            dash = "─" * max(0, w - 18)
            self._w(_dim(f"── Markers {dash}"), _eol())

        # Controls (centred)
        self._w(_goto(self._row(_REL_CONTROLS), 1))
        self._w(self._centre(ctrl, ctrl_vis), _eol())

        # Bottom border
        self._w(_goto(self._row(_REL_BORDER_BOT), 1))
        self._w(_bot_border(bc, w, ""), _eol())

        self._flush()
        # Now draw the dynamic parts
        self._last_state = None
        self._last_tc    = None
        self._last_pkts  = None
        self.refresh()

    def _audio_line(self) -> str:
        p = self.player
        if not self.args.audio:
            return _dim("—  none")
        if p._audio_error:
            return _fg(31, "✗") + "  " + p._audio_error
        if p._audio_loaded:
            dur = p.audio_duration_str()
            sr  = p._audio_samplerate
            name = os.path.basename(self.args.audio)
            return (_fg(32, "✓") + "  " + name
                    + _dim(f"  ({dur} @ {sr} Hz)"))
        return _fg(33, "Loading…")

    # ── Marker panel ──────────────────────────────────────────────────────────
    def _draw_markers(self) -> None:
        """Redraw all visible marker rows. Caller is responsible for flushing."""
        for i in range(self._n_vis):
            rel   = _REL_MARKERS_FIRST + i
            abs_i = self._marker_scroll + i
            if abs_i < len(self._markers):
                mid, name, tc = self._markers[abs_i]
                selected  = (abs_i == self._marker_cursor)
                arrow     = _bold(_fg(32, " ▶ ")) if selected else "   "
                id_str    = mid[:4].ljust(4)
                name_str  = name[:20].ljust(20)
                tc_str    = str(tc)
                content   = (arrow
                             + (_bold(id_str) if selected else _dim(id_str))
                             + "  " + (name_str if selected else _dim(name_str))
                             + "  " + tc_str)
            else:
                content = ""
            self._w(_goto(self._row(rel), 3))
            self._w(content, _eol())

        # Scroll indicators in the header row (right-aligned)
        hdr_row = self._row(_REL_MARKERS_HDR)
        up_ind = _dim("↑ ") if self._marker_scroll > 0 else "  "
        dn_ind = (_dim("↓")
                  if self._marker_scroll + self._n_vis < len(self._markers)
                  else " ")
        self._w(_goto(hdr_row, self._cols - 3))
        self._w(up_ind + dn_ind)

    def _clamp_scroll_to_cursor(self) -> None:
        if self._marker_cursor < self._marker_scroll:
            self._marker_scroll = self._marker_cursor
        elif self._marker_cursor >= self._marker_scroll + self._n_vis:
            self._marker_scroll = max(0, self._marker_cursor - self._n_vis + 1)

    def move_marker_cursor(self, delta: int) -> None:
        if not self._markers:
            return
        self._marker_cursor = max(0, min(len(self._markers) - 1,
                                         self._marker_cursor + delta))
        self._clamp_scroll_to_cursor()

    def selected_marker(self):
        """Return the currently selected (id, name, tc) tuple, or None."""
        if not self._markers:
            return None
        return self._markers[self._marker_cursor]

    # ── Dynamic refresh (only changed lines) ──────────────────────────────────
    def refresh(self) -> None:
        p     = self.player
        state = p.state
        tc    = str(p.get_tc())
        pkts  = p.packet_count
        col, label = STATE_ANSI[state]
        w     = self._cols

        changed = False

        # State label
        if state != self._last_state:
            self._w(_goto(self._row(_REL_STATE), 1))
            self._w(self._centre(_bold(_fg(col, label)), len(label)), _eol())
            # Redraw top border and side borders in new colour
            self._w(_goto(self._row(_REL_BORDER_TOP), 1))
            self._w(_top_border(col, w), _eol())
            for rel in range(2, UI_HEIGHT - 1):
                self._w(_goto(self._row(rel), 1),  _fg(col, "║"))
                self._w(_goto(self._row(rel), w),  _fg(col, "║"))
            self._last_state = state
            changed = True

        # Timecode
        if tc != self._last_tc:
            col, _ = STATE_ANSI[state]
            tc_display = _bold(_fg(col, tc))
            self._w(_goto(self._row(_REL_TC), 1))
            self._w(self._centre(tc_display, len(tc)), _eol())

            # Auto-track marker cursor to last marker at or before current TC
            if self._markers:
                cur_fn = p.get_tc().frame_number
                new_cursor = 0
                for i, (_, _, m_tc) in enumerate(self._markers):
                    if m_tc.frame_number <= cur_fn:
                        new_cursor = i
                    else:
                        break
                if new_cursor != self._marker_cursor:
                    self._marker_cursor = new_cursor
                    self._clamp_scroll_to_cursor()

            self._last_tc = tc
            changed = True

        # Packet counter
        if pkts != self._last_pkts:
            lw = 15
            self._w(_goto(self._row(_REL_PACKETS), 3))
            self._w(_dim("Packets sent".rjust(lw)), "  ", f"{pkts:,}", _eol())
            self._last_pkts = pkts
            changed = True

        # Bottom border with embedded status (redrawn every refresh so status stays current)
        self._w(_goto(self._row(_REL_BORDER_BOT), 1))
        self._w(_bot_border(col, w, p.status_msg), _eol())

        # Marker list (redrawn whenever cursor moves or on first draw)
        if self._markers and self._marker_cursor != self._last_cursor:
            self._draw_markers()
            self._last_cursor = self._marker_cursor

        # Restore right border for all content rows — _eol() calls in TC, packet,
        # and marker draws may have erased it; do this last so the border is final.
        for rel in range(2, UI_HEIGHT - 1):
            self._w(_goto(self._row(rel), w), _fg(col, "║"))
        self._flush()

    # ── Enter / exit ──────────────────────────────────────────────────────────
    def __enter__(self):
        self._w(_alt_screen_on(), _clear_screen())
        self._flush()
        self.draw_static()
        return self

    def __exit__(self, *_):
        self._w(_show_cursor(), _alt_screen_off())
        self._flush()


# ── Keyboard (cross-platform) ──────────────────────────────────────────────────
def _make_key_reader():
    """Return a platform-appropriate non-blocking key-read function."""
    if sys.platform == "win32":
        import msvcrt
        _WIN_ARROW = {"K": "\x1b[D", "M": "\x1b[C", "H": "\x1b[A", "P": "\x1b[B"}
        def read_key() -> Optional[str]:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    ch2 = msvcrt.getwch()   # always drain second byte
                    return _WIN_ARROW.get(ch2)  # None for unmapped specials
                return ch
            time.sleep(0.04)
            return None
    else:
        import tty, termios, select
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

        def read_key() -> Optional[str]:
            try:
                tty.setraw(fd)
                if select.select([sys.stdin], [], [], 0.04)[0]:
                    # Use os.read to bypass Python's BufferedReader, which would
                    # consume all 3 bytes of an arrow sequence on the first call,
                    # leaving subsequent select() checks seeing an empty fd.
                    ch = os.read(fd, 1).decode('latin-1')
                    if ch == "\x1b":
                        # Check for CSI escape sequence (arrow keys send \x1b[A/B/C/D)
                        if select.select([sys.stdin], [], [], 0.005)[0]:
                            ch2 = os.read(fd, 1).decode('latin-1')
                            if ch2 == "[" and select.select([sys.stdin], [], [], 0.005)[0]:
                                ch3 = os.read(fd, 1).decode('latin-1')
                                return "\x1b[" + ch3   # e.g. "\x1b[C" (right), "\x1b[D" (left)
                        return "\x1b"   # bare Escape or Alt+letter → treated as quit
                    return ch
                return None
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    return read_key


def _is_interactive() -> bool:
    """True if stdin is a real TTY (not piped/redirected)."""
    return sys.stdin.isatty()


# ── Argument parsing ───────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send SMPTE timecode over Art-Net (ArtTimeCode)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Frame rate choices:  24 | 25 | 29.97 | 30

Examples:
  # Default — 25fps to the standard Art-Net broadcast address
  python artnet_timecode.py

  # Custom IP and 30fps
  python artnet_timecode.py --ip 192.168.1.255 --fps 30

  # Start at 01:00:00:00 with audio
  python artnet_timecode.py --ip 192.168.1.255 --fps 25 \\
      --start-hours 1 --audio show.wav

  # Unicast to a single node
  python artnet_timecode.py --ip 192.168.1.42 --fps 25
        """)

    net = parser.add_argument_group("Network")
    net.add_argument("--ip",    default="2.255.255.255",
                     metavar="ADDR",
                     help="Destination IP (default: 2.255.255.255)")
    net.add_argument("--port",  type=int, default=ARTNET_PORT,
                     metavar="PORT",
                     help=f"UDP port (default: {ARTNET_PORT})")
    net.add_argument("--broadcast", action="store_true",
                     help="Force SO_BROADCAST on the socket")

    tc_g = parser.add_argument_group("Timecode")
    tc_g.add_argument("--fps", type=float, default=25.0,
                      choices=SUPPORTED_FPS, metavar="FPS",
                      help="Frame rate: 24 | 25 | 29.97 | 30  (default: 25)")
    tc_g.add_argument("--start-hours",   type=int, default=0, metavar="HH",
                      help="Start timecode hours   (default: 0)")
    tc_g.add_argument("--start-minutes", type=int, default=0, metavar="MM",
                      help="Start timecode minutes (default: 0)")
    tc_g.add_argument("--start-seconds", type=int, default=0, metavar="SS",
                      help="Start timecode seconds (default: 0)")
    tc_g.add_argument("--start-frames",  type=int, default=0, metavar="FF",
                      help="Start timecode frames  (default: 0)")

    parser.add_argument("--audio", metavar="FILE",
                        help="Audio file to play in sync (WAV, FLAC, OGG, AIFF…)")
    parser.add_argument("--markers", metavar="FILE",
                        help="CSV file of timecode markers (columns: #, Name, Start TC)")

    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    errs = []
    if args.fps not in SUPPORTED_FPS:
        errs.append(f"--fps must be one of {SUPPORTED_FPS}")
    if not (0 <= args.start_hours   <= 23):
        errs.append("--start-hours must be 0–23")
    if not (0 <= args.start_minutes <= 59):
        errs.append("--start-minutes must be 0–59")
    if not (0 <= args.start_seconds <= 59):
        errs.append("--start-seconds must be 0–59")
    max_frames = round(args.fps) - 1
    if not (0 <= args.start_frames <= max_frames):
        errs.append(f"--start-frames must be 0–{max_frames} for {args.fps} fps")
    if args.audio and not os.path.isfile(args.audio):
        errs.append(f"Audio file not found: {args.audio}")
    if errs:
        for e in errs:
            print(f"  ✗ {e}", file=sys.stderr)
        sys.exit(1)


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    args = parse_args()
    validate_args(args)

    # Auto-enable broadcast for .255 addresses
    if args.ip.endswith(".255"):
        args.broadcast = True

    start_tc = make_tc(
        fps     = args.fps,
        hours   = args.start_hours,
        minutes = args.start_minutes,
        seconds = args.start_seconds,
        frames  = args.start_frames,
    )

    player = ArtNetTimecodePlayer(
        start_tc   = start_tc,
        fps        = args.fps,
        dest_ip    = args.ip,
        dest_port  = args.port,
        audio_path = args.audio,
        broadcast  = args.broadcast,
    )

    console = Console() if Console else None

    # ── Non-interactive fallback (piped stdin / CI) ────────────────────────────
    if not _is_interactive():
        print("Non-interactive mode — auto-playing. Send SIGINT to stop.")
        player.play()
        try:
            while True:
                time.sleep(0.5)
                print(f"\r  {player.get_tc()}", end="", flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            player.stop()
            player.shutdown()
        return

    # ── Marker file ────────────────────────────────────────────────────────────
    markers = []
    if args.markers:
        markers = load_markers(args.markers, args.fps)
    if markers:
        import shutil as _shutil
        t_rows = _shutil.get_terminal_size().lines
        n_vis  = max(3, min(len(markers), t_rows - 18))
        _configure_markers_layout(n_vis)

    # ── Interactive TUI ────────────────────────────────────────────────────────
    read_key = _make_key_reader()

    with DirectTUI(player, args, markers=markers) as tui:
        try:
            while True:
                key = read_key()
                if key is not None:
                    k = key.lower()
                    if k == " ":
                        player.toggle_play_pause()
                    elif k == "s":
                        player.stop()
                    elif k == "\x1b[c":   # right arrow → +5s
                        player.scrub(+5.0)
                    elif k == "\x1b[d":   # left arrow  → −5s
                        player.scrub(-5.0)
                    elif k == "\x1b[a":   # up arrow → previous marker
                        tui.move_marker_cursor(-1)
                    elif k == "\x1b[b":   # down arrow → next marker
                        tui.move_marker_cursor(+1)
                    elif k == "\r":       # Enter → jump to selected marker
                        m = tui.selected_marker()
                        if m:
                            player.seek_to_frame(m[2].frame_number)
                    elif k in ("q", "\x03", "\x1b"):   # q / Ctrl-C / Esc
                        break
                tui.refresh()
        except KeyboardInterrupt:
            pass
        finally:
            player.stop()
            player.shutdown()

    print(f"\nStopped. {player.packet_count:,} Art-Net packets sent.\n")


if __name__ == "__main__":
    main()
