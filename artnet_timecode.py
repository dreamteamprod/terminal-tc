#!/usr/bin/env python3
"""
ArtNet Timecode Player
======================
Sends SMPTE timecode over Art-Net (ArtTimeCode) and optionally
plays an audio file in sync.

Cross-platform: Linux, macOS, Windows
Dependencies: timecode, sounddevice, soundfile, numpy, textual

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
ARTNET_ID = b"Art-Net\x00"
OP_TIMECODE = 0x9700  # ArtTimeCode opcode

SUPPORTED_FPS = [24, 25, 29.97, 30]

FPS_TYPE_MAP = {  # Art-Net type field values
    24: 0,
    25: 1,
    29.97: 2,
    30: 3,
}

FPS_LABEL = {
    24: "24 fps  (Film / DCI)",
    25: "25 fps  (EBU / PAL)",
    29.97: "29.97 fps  (Drop Frame / NTSC)",
    30: "30 fps  (SMPTE / HD)",
}


# ── Art-Net packet builder ─────────────────────────────────────────────────────
def build_artimecode(
    hours: int, minutes: int, seconds: int, frames: int, fps_type: int
) -> bytes:
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
        + struct.pack("BBBBBB", frames, seconds, minutes, hours, fps_type, 0)
    )


# ── Timecode helpers (wrapping the `timecode` library) ────────────────────────
# The `timecode` library uses a 1-indexed `frames` constructor argument:
#   Timecode(fps, frames=1)  →  00:00:00:00
#   Timecode(fps, frames=2)  →  00:00:00:01
# Its `frame_number` property is 0-indexed (00:00:00:00 → frame_number=0).
# We keep these details internal so the rest of the code stays clean.

# fps argument the library accepts as a string
_FPS_STR = {
    24: "24",
    25: "25",
    29.97: "29.97",
    30: "30",
}


def make_tc(
    fps: float, hours: int, minutes: int, seconds: int, frames: int
) -> _LibTimecode:
    """Construct a Timecode from H/M/S/F components."""
    sep = ";" if fps == 29.97 else ":"
    tc_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{frames:02d}"
    return _LibTimecode(_FPS_STR[fps], tc_str)


def tc_from_frame_number(fps: float, frame_number: int) -> _LibTimecode:
    """Construct a Timecode from a 0-based absolute frame number."""
    return _LibTimecode(_FPS_STR[fps], frames=frame_number + 1)


def _detect_marker_format(path: str) -> str:
    """Sniff the first non-empty line to determine marker file format."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    return "reaper"
                if line.startswith("Track"):
                    return "cuepoints"
                return "audacity"
    except OSError:
        pass
    return "reaper"


def _parse_reaper_markers(f, fps: float) -> list:
    import csv
    markers = []
    for row in csv.reader(f):
        if len(row) < 3 or row[0].strip() == "#":
            continue
        mid, name, tc_str = row[0].strip(), row[1].strip(), row[2].strip()
        try:
            markers.append((mid, name, _LibTimecode(_FPS_STR[fps], tc_str)))
        except Exception:
            pass
    return markers


def _parse_audacity_markers(f, fps: float) -> list:
    markers = []
    seq = 1
    for line in f:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 3:
            continue
        try:
            frame_number = round(float(parts[0]) * fps)
            tc = tc_from_frame_number(fps, frame_number)
            markers.append((str(seq), parts[2].strip(), tc))
            seq += 1
        except (ValueError, KeyError):
            pass
    return markers


def _parse_cuepoints_markers(f, fps: float) -> list:
    import csv
    markers = []
    for row in csv.reader(f, delimiter="\t"):
        if len(row) < 5 or row[0].strip() == "Track":
            continue
        mid, tc_str, name = row[3].strip(), row[2].strip(), row[4].strip()
        try:
            markers.append((mid, name, _LibTimecode(_FPS_STR[fps], tc_str)))
        except Exception:
            pass
    return markers


def load_markers(path: str, fps: float, fmt: str = "auto") -> list:
    """Load cue markers from a file, returning (id, name, tc) tuples sorted by timecode.

    Supported formats: reaper, audacity, cuepoints (default: auto-detect).
    """
    if fmt == "auto":
        fmt = _detect_marker_format(path)
    try:
        with open(path, newline="", encoding="utf-8") as f:
            if fmt == "audacity":
                markers = _parse_audacity_markers(f, fps)
            elif fmt == "cuepoints":
                markers = _parse_cuepoints_markers(f, fps)
            else:
                markers = _parse_reaper_markers(f, fps)
    except OSError as e:
        sys.exit(f"Cannot open markers file: {e}")
    markers.sort(key=lambda m: m[2].frame_number)
    return markers


# ── State ──────────────────────────────────────────────────────────────────────
class State(IntEnum):
    STOPPED = 0
    PLAYING = 1
    PAUSED = 2


STATE_STYLE = {
    State.STOPPED: ("red", "■  STOPPED"),
    State.PLAYING: ("green", "▶  PLAYING"),
    State.PAUSED: ("yellow", "⏸  PAUSED"),
}


# ── Player ─────────────────────────────────────────────────────────────────────
class ArtNetTimecodePlayer:
    def __init__(
        self,
        start_tc: _LibTimecode,
        fps: float,
        dest_ip: str,
        dest_port: int,
        audio_path: Optional[str] = None,
        broadcast: bool = False,
    ):

        self.start_tc = start_tc
        self.fps = fps
        self.dest_ip = dest_ip
        self.dest_port = dest_port
        self.audio_path = audio_path
        self.broadcast = broadcast

        self.fps_type = FPS_TYPE_MAP.get(fps, 3)
        self._frame_interval = 1.0 / fps
        self.state = State.STOPPED
        self.packet_count = 0
        self.error_count = 0
        self.status_msg = "Ready"

        # Current displayed timecode
        self._tc_lock = threading.Lock()
        self._tc: _LibTimecode = start_tc

        # Pause/resume state tracking
        self._play_start_wall: float = 0.0
        self._pause_frame_acc: int = 0  # frames already elapsed before pause

        self._stop_event = threading.Event()
        self._ticker_thread: Optional[threading.Thread] = None

        # UDP socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if broadcast:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        # Audio
        self._audio_data: Optional[np.ndarray] = None
        self._audio_samplerate: int = 44100
        self._audio_thread_hdl: Optional[threading.Thread] = None
        self._audio_pos: int = 0
        self._audio_loaded: bool = False
        self._audio_error: str = ""
        self._audio_channels: int = 2

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
            self._audio_data = data
            self._audio_samplerate = sr
            self._audio_channels = data.shape[1]
            self._audio_loaded = True
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
                data = self._audio_data
                total = len(data)
                pos = start_pos
                while not self._stop_event.is_set() and pos < total:
                    chunk = data[pos : pos + chunk_size]
                    stream.write(chunk)  # blocks in C, GIL released during wait
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
        interval = self._frame_interval
        wall_origin = self._play_start_wall
        start_fn = self.start_tc.frame_number + self._pause_frame_acc
        next_tick = wall_origin
        local_fn = start_fn

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
                pkt = build_artimecode(tc.hrs, tc.mins, tc.secs, tc.frs, self.fps_type)
                self._sock.sendto(pkt, (self.dest_ip, self.dest_port))
                self.packet_count += 1
            except Exception:
                self.error_count += 1

            local_fn += 1
            next_tick = wall_origin + (local_fn - start_fn) * interval

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
                (self._pause_frame_acc / self.fps) * self._audio_samplerate
            )
            self._start_audio_at(audio_offset)

        self._ticker_thread = threading.Thread(target=self._ticker, daemon=True)
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
            old_audio = self._audio_thread_hdl
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
            self.state = State.PAUSED
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
        was_playing = self.state == State.PLAYING

        if was_playing:
            old_ticker = self._ticker_thread
            old_audio = self._audio_thread_hdl
            self.pause()
            if old_ticker is not None:
                old_ticker.join(timeout=0.2)
            if old_audio is not None:
                old_audio.join(timeout=0.2)
        elif self.state == State.STOPPED:
            self.state = State.PAUSED
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
        """,
    )

    net = parser.add_argument_group("Network")
    net.add_argument(
        "--ip",
        default="2.255.255.255",
        metavar="ADDR",
        help="Destination IP (default: 2.255.255.255)",
    )
    net.add_argument(
        "--port",
        type=int,
        default=ARTNET_PORT,
        metavar="PORT",
        help=f"UDP port (default: {ARTNET_PORT})",
    )
    net.add_argument(
        "--broadcast", action="store_true", help="Force SO_BROADCAST on the socket"
    )

    tc_g = parser.add_argument_group("Timecode")
    tc_g.add_argument(
        "--fps",
        type=float,
        default=25.0,
        choices=SUPPORTED_FPS,
        metavar="FPS",
        help="Frame rate: 24 | 25 | 29.97 | 30  (default: 25)",
    )
    tc_g.add_argument(
        "--start-hours",
        type=int,
        default=0,
        metavar="HH",
        help="Start timecode hours   (default: 0)",
    )
    tc_g.add_argument(
        "--start-minutes",
        type=int,
        default=0,
        metavar="MM",
        help="Start timecode minutes (default: 0)",
    )
    tc_g.add_argument(
        "--start-seconds",
        type=int,
        default=0,
        metavar="SS",
        help="Start timecode seconds (default: 0)",
    )
    tc_g.add_argument(
        "--start-frames",
        type=int,
        default=0,
        metavar="FF",
        help="Start timecode frames  (default: 0)",
    )

    parser.add_argument(
        "--audio",
        metavar="FILE",
        help="Audio file to play in sync (WAV, FLAC, OGG, AIFF…)",
    )
    parser.add_argument(
        "--markers",
        metavar="FILE",
        help="Marker file (Reaper CSV, Audacity labels, or CuePoints spreadsheet; auto-detected)",
    )
    parser.add_argument(
        "--marker-format",
        choices=["auto", "reaper", "audacity", "cuepoints"],
        default="auto",
        metavar="FMT",
        help="Marker file format: auto (default), reaper, audacity, cuepoints",
    )

    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    errs = []
    if args.fps not in SUPPORTED_FPS:
        errs.append(f"--fps must be one of {SUPPORTED_FPS}")
    if not (0 <= args.start_hours <= 23):
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
        fps=args.fps,
        hours=args.start_hours,
        minutes=args.start_minutes,
        seconds=args.start_seconds,
        frames=args.start_frames,
    )

    player = ArtNetTimecodePlayer(
        start_tc=start_tc,
        fps=args.fps,
        dest_ip=args.ip,
        dest_port=args.port,
        audio_path=args.audio,
        broadcast=args.broadcast,
    )

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
        markers = load_markers(args.markers, args.fps, fmt=args.marker_format)

    # ── Interactive TUI ────────────────────────────────────────────────────────
    from tui_app import TimecodeApp

    TimecodeApp(player, args, markers=markers).run()
    print(f"\nStopped. {player.packet_count:,} Art-Net packets sent.\n")


if __name__ == "__main__":
    main()
