"""
Persistent configuration for the Art-Net Timecode Player.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import os
import socket
from typing import Optional

SUPPORTED_FPS = [24, 25, 29.97, 30]

CONFIG_DIR = os.path.expanduser("~/.config/artnet-timecode")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


def get_default_broadcast() -> str:
    """Detect the broadcast address for the current primary network interface.

    Uses the outbound-route trick to find the local IP, then reads the real
    netmask via psutil to compute the correct broadcast address.  Falls back
    to a /24 estimate, then to 2.255.255.255 (Art-Net global default).
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
    except Exception:
        return "2.255.255.255"

    try:
        import psutil

        for addrs in psutil.net_if_addrs().values():
            for addr in addrs:
                if addr.family == socket.AF_INET and addr.address == local_ip:
                    if addr.broadcast:
                        return addr.broadcast
                    if addr.netmask:
                        net = ipaddress.ip_network(
                            f"{local_ip}/{addr.netmask}", strict=False
                        )
                        return str(net.broadcast_address)
    except Exception:
        pass

    # /24 estimate — correct for most home/office LANs
    parts = local_ip.split(".")
    return f"{parts[0]}.{parts[1]}.{parts[2]}.255"


@dataclasses.dataclass
class TrackConfig:
    name: str = "Track 1"
    audio: Optional[str] = None
    markers: Optional[str] = None
    marker_format: str = "auto"
    start_hours: int = 0
    start_minutes: int = 0
    start_seconds: int = 0
    start_frames: int = 0
    markers_absolute: bool = True
    stop_on_audio_end: Optional[bool] = None  # None = inherit global default


@dataclasses.dataclass
class AppConfig:
    ip: str = dataclasses.field(default_factory=get_default_broadcast)
    port: int = 6454
    broadcast: bool = False
    fps: float = 25.0
    project_name: str = "Default"
    # Kept for CLI backwards compat — single-track path in main()
    start_hours: int = 0
    start_minutes: int = 0
    start_seconds: int = 0
    start_frames: int = 0
    audio: Optional[str] = None
    markers: Optional[str] = None
    marker_format: str = "auto"
    tracks: list = dataclasses.field(default_factory=list)  # list[TrackConfig]
    reset_tc_on_stop: bool = True
    stop_on_audio_end: bool = False
    osc_enabled: bool = False
    osc_port: int = 9000
    tc_offset_frames: int = 0


def _config_from_dict(data: dict) -> AppConfig:
    """Parse a raw JSON dict into an AppConfig with proper TrackConfig objects."""
    known_app = {field.name for field in dataclasses.fields(AppConfig)}
    known_track = {field.name for field in dataclasses.fields(TrackConfig)}
    filtered = {k: v for k, v in data.items() if k in known_app}
    if "fps" in filtered:
        filtered["fps"] = float(filtered["fps"])
    # dataclasses.asdict() serialises TrackConfig as plain dicts — re-inflate manually
    raw_tracks = filtered.pop("tracks", [])
    tracks = [
        TrackConfig(**{k: v for k, v in t.items() if k in known_track})
        for t in raw_tracks
        if isinstance(t, dict)
    ]
    cfg = AppConfig(**filtered)
    cfg.tracks = tracks
    return cfg


def load_config() -> AppConfig:
    """Load persisted config. Returns AppConfig() with defaults on any error."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        cfg = _config_from_dict(data)
    except (OSError, json.JSONDecodeError, TypeError):
        cfg = AppConfig()

    # Migrate legacy single-track config (no tracks list) to tracks[0]
    if not cfg.tracks:
        cfg.tracks = [
            TrackConfig(
                name="Track 1",
                audio=cfg.audio,
                markers=cfg.markers,
                marker_format=cfg.marker_format,
                start_hours=cfg.start_hours,
                start_minutes=cfg.start_minutes,
                start_seconds=cfg.start_seconds,
                start_frames=cfg.start_frames,
            )
        ]

    return cfg


def save_config(cfg: AppConfig) -> None:
    """Atomically persist config to disk."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dataclasses.asdict(cfg), f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def validate_config(cfg: AppConfig, *, check_files: bool = True) -> list[str]:
    """Return human-readable error strings for global (non-track) settings; empty list means valid."""
    errs: list[str] = []
    if cfg.fps not in SUPPORTED_FPS:
        errs.append(f"Frame rate must be one of {SUPPORTED_FPS}")
    if not (0 <= cfg.start_hours <= 23):
        errs.append("Start hours must be 0–23")
    if not (0 <= cfg.start_minutes <= 59):
        errs.append("Start minutes must be 0–59")
    if not (0 <= cfg.start_seconds <= 59):
        errs.append("Start seconds must be 0–59")
    max_frames = round(cfg.fps) - 1
    if not (0 <= cfg.start_frames <= max_frames):
        errs.append(f"Start frames must be 0–{max_frames} for {cfg.fps} fps")
    if check_files:
        if cfg.audio and not os.path.isfile(cfg.audio):
            errs.append(f"Audio file not found: {cfg.audio}")
        if cfg.markers and not os.path.isfile(cfg.markers):
            errs.append(f"Markers file not found: {cfg.markers}")
    return errs


def validate_track_config(
    track: TrackConfig, fps: float, *, check_files: bool = True
) -> list[str]:
    """Return human-readable error strings for a single track; empty list means valid."""
    errs: list[str] = []
    if not track.name.strip():
        errs.append("Track name cannot be empty")
    if not (0 <= track.start_hours <= 23):
        errs.append("Start hours must be 0–23")
    if not (0 <= track.start_minutes <= 59):
        errs.append("Start minutes must be 0–59")
    if not (0 <= track.start_seconds <= 59):
        errs.append("Start seconds must be 0–59")
    max_frames = round(fps) - 1
    if not (0 <= track.start_frames <= max_frames):
        errs.append(f"Start frames must be 0–{max_frames} for {fps} fps")
    if check_files:
        if track.audio and not os.path.isfile(track.audio):
            errs.append(f"Audio file not found: {track.audio}")
        if track.markers and not os.path.isfile(track.markers):
            errs.append(f"Markers file not found: {track.markers}")
    return errs
