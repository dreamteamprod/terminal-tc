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
import dataclasses
import socket
import struct
import sys
import threading
import time
import os
from enum import IntEnum
from typing import Optional

from config import AppConfig, SUPPORTED_FPS, load_config, validate_config

# ── Third-party ───────────────────────────────────────────────────────────────
try:
    from timecode import Timecode as _LibTimecode
except ImportError:
    sys.exit("Missing dependency: pip install timecode")

try:
    import numpy as np
    _NP_AVAILABLE = True
except ImportError:
    _NP_AVAILABLE = False

try:
    import sounddevice as sd
    import soundfile as sf
    AUDIO_AVAILABLE = _NP_AVAILABLE
except ImportError:
    AUDIO_AVAILABLE = False
except OSError:
    # PortAudio not installed (common on WSL/headless Linux)
    AUDIO_AVAILABLE = False

# ── Helpers ───────────────────────────────────────────────────────────────────
def _is_wsl() -> bool:
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _wsl_audio_hint() -> str:
    """Return a fix hint when no audio output devices are found on WSL."""
    import glob
    plugin = glob.glob("/usr/lib/*/alsa-lib/libasound_module_pcm_pulse.so")
    if not plugin:
        return (
            "No audio devices on WSL — fix: sudo apt install libasound2-plugins"
        )
    return "No audio devices — ALSA pulse plugin present but PortAudio sees no outputs"


# ── Constants ──────────────────────────────────────────────────────────────────
ARTNET_PORT = 6454
ARTNET_ID = b"Art-Net\x00"
OP_TIMECODE = 0x9700  # ArtTimeCode opcode

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
            if _is_wsl():
                self._audio_error = (
                    "Audio unavailable on WSL — "
                    "sudo apt install libportaudio2 libsndfile1 && "
                    "pip install sounddevice soundfile"
                )
            else:
                self._audio_error = "Audio unavailable — pip install sounddevice soundfile"
            return
        try:
            has_output = any(d["max_output_channels"] > 0 for d in sd.query_devices())
        except Exception:
            has_output = True  # can't check; proceed and let playback fail naturally
        if not has_output:
            self._audio_error = _wsl_audio_hint() if _is_wsl() else "No audio output devices found"
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
        default=argparse.SUPPRESS,
        metavar="ADDR",
        help="Destination IP (default: auto-detected broadcast)",
    )
    net.add_argument(
        "--port",
        type=int,
        default=argparse.SUPPRESS,
        metavar="PORT",
        help=f"UDP port (default: {ARTNET_PORT})",
    )
    net.add_argument(
        "--broadcast",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Force SO_BROADCAST on the socket",
    )

    tc_g = parser.add_argument_group("Timecode")
    tc_g.add_argument(
        "--fps",
        type=float,
        default=argparse.SUPPRESS,
        choices=SUPPORTED_FPS,
        metavar="FPS",
        help="Frame rate: 24 | 25 | 29.97 | 30  (default: 25)",
    )
    tc_g.add_argument(
        "--start-hours",
        type=int,
        default=argparse.SUPPRESS,
        metavar="HH",
        help="Start timecode hours   (default: 0)",
    )
    tc_g.add_argument(
        "--start-minutes",
        type=int,
        default=argparse.SUPPRESS,
        metavar="MM",
        help="Start timecode minutes (default: 0)",
    )
    tc_g.add_argument(
        "--start-seconds",
        type=int,
        default=argparse.SUPPRESS,
        metavar="SS",
        help="Start timecode seconds (default: 0)",
    )
    tc_g.add_argument(
        "--start-frames",
        type=int,
        default=argparse.SUPPRESS,
        metavar="FF",
        help="Start timecode frames  (default: 0)",
    )

    parser.add_argument(
        "--audio",
        default=argparse.SUPPRESS,
        metavar="FILE",
        help="Audio file to play in sync (WAV, FLAC, OGG, AIFF…)",
    )
    parser.add_argument(
        "--markers",
        default=argparse.SUPPRESS,
        metavar="FILE",
        help="Marker file (Reaper CSV, Audacity labels, or CuePoints spreadsheet; auto-detected)",
    )
    parser.add_argument(
        "--marker-format",
        choices=["auto", "reaper", "audacity", "cuepoints"],
        default=argparse.SUPPRESS,
        metavar="FMT",
        help="Marker file format: auto (default), reaper, audacity, cuepoints",
    )

    return parser.parse_args()


def build_player(cfg: AppConfig) -> "ArtNetTimecodePlayer":
    """Construct a player from a fully-merged AppConfig."""
    start_tc = make_tc(
        fps=cfg.fps,
        hours=cfg.start_hours,
        minutes=cfg.start_minutes,
        seconds=cfg.start_seconds,
        frames=cfg.start_frames,
    )
    return ArtNetTimecodePlayer(
        start_tc=start_tc,
        fps=cfg.fps,
        dest_ip=cfg.ip,
        dest_port=cfg.port,
        audio_path=cfg.audio,
        broadcast=cfg.broadcast,
    )


def build_markers(cfg: AppConfig) -> list:
    """Load marker file described by cfg, or return [] if none configured."""
    if not cfg.markers:
        return []
    return load_markers(cfg.markers, cfg.fps, fmt=cfg.marker_format)


def build_player_from_track(track, cfg: AppConfig) -> "ArtNetTimecodePlayer":
    """Construct a player for a TrackConfig using global network/FPS settings."""
    start_tc = make_tc(
        fps=cfg.fps,
        hours=track.start_hours,
        minutes=track.start_minutes,
        seconds=track.start_seconds,
        frames=track.start_frames,
    )
    return ArtNetTimecodePlayer(
        start_tc=start_tc,
        fps=cfg.fps,
        dest_ip=cfg.ip,
        dest_port=cfg.port,
        audio_path=track.audio,
        broadcast=cfg.broadcast,
    )


def build_markers_from_track(track, fps: float) -> list:
    """Load marker file for a TrackConfig, or return [] if none configured."""
    if not track.markers:
        return []
    markers = load_markers(track.markers, fps, fmt=track.marker_format)
    if not track.markers_absolute:
        offset = make_tc(
            fps,
            track.start_hours,
            track.start_minutes,
            track.start_seconds,
            track.start_frames,
        ).frame_number
        markers = [
            (mid, name, tc_from_frame_number(fps, tc.frame_number + offset))
            for mid, name, tc in markers
        ]
    return markers


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    # Three-layer merge: defaults → saved JSON → explicit CLI flags.
    # Tracks are handled separately: load_config() returns proper TrackConfig
    # objects; asdict() would flatten them back to dicts and lose the type.
    cli_dict = vars(parse_args())
    saved_config = load_config()  # already has TrackConfig objects + migration applied
    defaults_dict = dataclasses.asdict(AppConfig())
    saved_flat = dataclasses.asdict(saved_config)
    saved_flat.pop("tracks", None)
    defaults_dict.pop("tracks", None)
    cli_dict.pop("tracks", None)
    config = AppConfig(**{**defaults_dict, **saved_flat, **cli_dict})
    config.tracks = saved_config.tracks  # preserve the properly-typed TrackConfig list

    # Auto-enable broadcast for .255 addresses
    if config.ip.endswith(".255"):
        config.broadcast = True

    # Always-fatal: numeric / fps / TC-range constraint violations
    errs = validate_config(config, check_files=False)
    if errs:
        for e in errs:
            print(f"  ✗ {e}", file=sys.stderr)
        sys.exit(1)

    # File paths passed explicitly on the CLI → fatal if missing
    if "audio" in cli_dict and config.audio and not os.path.isfile(config.audio):
        print(f"  ✗ Audio file not found: {config.audio}", file=sys.stderr)
        sys.exit(1)
    if "markers" in cli_dict and config.markers and not os.path.isfile(config.markers):
        print(f"  ✗ Markers file not found: {config.markers}", file=sys.stderr)
        sys.exit(1)

    # Stale saved-config file paths → clear silently so TUI can open
    if config.audio and not os.path.isfile(config.audio):
        config.audio = None
    if config.markers and not os.path.isfile(config.markers):
        config.markers = None

    # If CLI supplied track-related flags, sync them into tracks[0] so they win
    _track_cli_keys = {"audio", "markers", "marker_format", "start_hours", "start_minutes", "start_seconds", "start_frames"}
    if _track_cli_keys & cli_dict.keys():
        t0 = config.tracks[0]
        if "audio" in cli_dict:
            t0.audio = config.audio
        if "markers" in cli_dict:
            t0.markers = config.markers
        if "marker_format" in cli_dict:
            t0.marker_format = config.marker_format
        for k in ("start_hours", "start_minutes", "start_seconds", "start_frames"):
            if k in cli_dict:
                setattr(t0, k, getattr(config, k))

    initial_track = config.tracks[0]
    player = build_player_from_track(initial_track, config)

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

    # ── Interactive TUI ────────────────────────────────────────────────────────
    from tui_app import TimecodeApp

    markers = build_markers_from_track(initial_track, config.fps)

    TimecodeApp(config, player, markers=markers, tracks=config.tracks).run()
    print(f"\nStopped. {player.packet_count:,} Art-Net packets sent.\n")


if __name__ == "__main__":
    main()
