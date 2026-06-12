"""
Project management: local named saves and portable .tcp export/import.

A project is an AppConfig plus a name.  Local saves live in PROJECT_DIR as
plain JSON (same schema as config.json).  Exported bundles are ZIP files
(.tcp) that embed audio and marker files so the project is self-contained.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import zipfile
from pathlib import Path

from .config import AppConfig, TrackConfig, _config_from_dict, save_config

PROJECT_DIR = Path.home() / ".config" / "artnet-timecode" / "projects"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9 _.\-]{1,64}$")


def _safe_project_path(name: str) -> Path:
    """Return the resolved .json path for name, or raise ValueError if unsafe."""
    if not name or not _SAFE_NAME.match(name) or name in (".", ".."):
        raise ValueError(f"Invalid project name: {name!r}")
    candidate = (PROJECT_DIR / f"{name}.json").resolve()
    if candidate.parent != PROJECT_DIR.resolve():
        raise ValueError(f"Project name escapes project directory: {name!r}")
    return candidate


def list_projects() -> list[str]:
    """Return sorted list of saved project names (without the .json extension)."""
    if not PROJECT_DIR.exists():
        return []
    return sorted(p.stem for p in PROJECT_DIR.iterdir() if p.suffix == ".json")


def load_project(name: str) -> AppConfig:
    """Load a named project from PROJECT_DIR. Raises FileNotFoundError if absent."""
    path = _safe_project_path(name)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    cfg = _config_from_dict(data)
    if not cfg.tracks:
        cfg.tracks = [TrackConfig()]
    return cfg


def save_project(cfg: AppConfig) -> None:
    """Write the project to PROJECT_DIR/<name>.json and update the active config.json."""
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    path = _safe_project_path(cfg.project_name or "Default")
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dataclasses.asdict(cfg), f, indent=2)
    os.replace(tmp, str(path))
    save_config(cfg)


def delete_project(name: str) -> None:
    """Delete a named project file. Raises FileNotFoundError if absent."""
    _safe_project_path(name).unlink()


def export_project(cfg: AppConfig, output_path: Path) -> None:
    """Bundle the project config, audio files, and marker files into a .tcp ZIP."""
    portable = dataclasses.asdict(cfg)
    # Maps original absolute path → relative path inside the ZIP
    used_audio: dict[str, str] = {}
    used_markers: dict[str, str] = {}

    for track in portable["tracks"]:
        orig_audio = track.get("audio")
        if orig_audio and os.path.isfile(orig_audio):
            if orig_audio not in used_audio:
                fname = _unique_filename(
                    os.path.basename(orig_audio),
                    set(used_audio.values()),
                )
                used_audio[orig_audio] = f"audio/{fname}"
            track["audio"] = used_audio[orig_audio]
        else:
            track["audio"] = None

        orig_markers = track.get("markers")
        if orig_markers and os.path.isfile(orig_markers):
            if orig_markers not in used_markers:
                fname = _unique_filename(
                    os.path.basename(orig_markers),
                    set(used_markers.values()),
                )
                used_markers[orig_markers] = f"markers/{fname}"
            track["markers"] = used_markers[orig_markers]
        else:
            track["markers"] = None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project.json", json.dumps(portable, indent=2))
        for abs_path, rel_path in used_audio.items():
            zf.write(abs_path, rel_path)
        for abs_path, rel_path in used_markers.items():
            zf.write(abs_path, rel_path)


def import_project(tcp_path: Path, extract_dir: Path) -> AppConfig:
    """Extract a .tcp bundle into extract_dir and return a ready-to-use AppConfig."""
    extract_dir.mkdir(parents=True, exist_ok=True)
    base = extract_dir.resolve()

    with zipfile.ZipFile(tcp_path, "r") as zf:
        for member in zf.infolist():
            name = member.filename
            # Reject absolute paths, Windows drive letters, and backslash prefixes
            if name.startswith(("/", "\\")) or os.path.isabs(name) or ":" in name:
                raise ValueError(f"Unsafe path in archive: {name!r}")
            target = (base / name).resolve()
            # Reject paths that escape the extract directory (Zip Slip)
            if target != base and not str(target).startswith(str(base) + os.sep):
                raise ValueError(f"Path traversal detected in archive: {name!r}")
        zf.extractall(extract_dir)

    with open(extract_dir / "project.json", encoding="utf-8") as f:
        data = json.load(f)

    known_track = {field.name for field in dataclasses.fields(TrackConfig)}
    raw_tracks = data.pop("tracks", [])
    cfg = _config_from_dict(data)

    tracks = []
    for t in raw_tracks:
        if not isinstance(t, dict):
            continue
        fields = {k: v for k, v in t.items() if k in known_track}
        for key in ("audio", "markers"):
            rel = fields.get(key)
            if not rel:
                continue
            resolved = (base / rel).resolve()
            # Only accept paths that land inside the extract directory
            if resolved != base and not str(resolved).startswith(str(base) + os.sep):
                raise ValueError(
                    f"Track {key!r} path escapes extract directory: {rel!r}"
                )
            fields[key] = str(resolved)
        tracks.append(TrackConfig(**fields))

    cfg.tracks = tracks or [TrackConfig()]

    # Ensure the imported project_name is safe before the caller calls save_project.
    # A crafted bundle could set project_name to e.g. "../../evil" to escape PROJECT_DIR.
    try:
        _safe_project_path(cfg.project_name)
    except ValueError:
        cfg.project_name = "Imported Project"

    return cfg


def _unique_filename(basename: str, existing_relpaths: set[str]) -> str:
    """Return a filename that doesn't collide with any basename in existing_relpaths."""
    existing_names = {os.path.basename(p) for p in existing_relpaths}
    if basename not in existing_names:
        return basename
    stem, ext = os.path.splitext(basename)
    i = 1
    while True:
        candidate = f"{stem}_{i}{ext}"
        if candidate not in existing_names:
            return candidate
        i += 1
