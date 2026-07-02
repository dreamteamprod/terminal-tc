# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
# Install (once — sets up the artnet-timecode entry point)
pip install -e ".[dev]"   # [dev] adds pytest

# Launch the interactive TUI (reads saved config automatically)
artnet-timecode

# Equivalent module invocation
python -m terminal_tc

# Common CLI overrides
artnet-timecode --ip 192.168.1.255 --fps 30
artnet-timecode --fps 25 --start-hours 1 --audio show.wav --markers cues.csv

# Run marker-generator unit tests
pytest tests/
```

## Architecture

All source lives in the `terminal_tc/` package. Six files, each with a single responsibility:

- **`terminal_tc/__init__.py`** — Package marker and `__version__`.
- **`terminal_tc/__main__.py`** — Enables `python -m terminal_tc`.
- **`terminal_tc/config.py`** — `AppConfig` and `TrackConfig` dataclasses, JSON persistence (`~/.config/artnet-timecode/config.json`), and `validate_config`. Imported by all other modules.
- **`terminal_tc/artnet_timecode.py`** — The headless player engine: Art-Net UDP packet builder, timecode helpers (wrapping the `timecode` library), multi-format marker file parsing, and `ArtNetTimecodePlayer` (threading, audio, transport controls). Also contains `main()`, CLI argument parsing, and the three-layer config merge (defaults → saved JSON → explicit CLI flags). In non-interactive mode (piped stdin) it auto-plays without the TUI.
- **`terminal_tc/marker_gen.py`** — Pure-math BPM/time-signature marker generator. No UI or network imports; uses `fractions.Fraction` for exact frame arithmetic. Key entry points: `generate_markers()` and `validate_marker_gen_params()`.
- **`terminal_tc/tui_app.py`** — [Textual](https://textual.textualize.io/) TUI. `TimecodeApp` receives a fully-constructed `ArtNetTimecodePlayer` and a markers list; it never touches networking or audio directly. A 30 Hz poll loop (`set_interval`) updates the display from player state. Includes `ModalScreen`-based settings, export, and marker-generator screens; a command palette (`TimecodeCommands`); `WaveformWidget` (half-block Unicode rendering); and `MarkerList` (scrollable cursor widget).

Tests live in `tests/test_marker_gen.py` (49 tests, all pure arithmetic — no UI or network).

## Key design details

**Config merge order** (`main()`): CLI flags win over saved JSON, which wins over dataclass defaults. `argparse.SUPPRESS` is used so only explicitly-passed flags override saved config.

**Timecode library quirk**: The `timecode` library uses 1-indexed `frames` constructor argument (`frames=1` → `00:00:00:00`), but its `.frame_number` property is 0-indexed. `make_tc()` and `tc_from_frame_number()` encapsulate this.

**Frame ticker accuracy**: The ticker thread uses absolute time anchoring (`wall_origin + n * interval`) rather than accumulated sleeps, so timing error does not drift over long runs. A busy-spin covers the final millisecond.

**Marker formats**: Three supported formats auto-detected by sniffing the first non-empty line — Reaper CSV (starts with `#`), CuePoints TSV (starts with `Track`), and Audacity labels (everything else). All normalise to `(id, name, Timecode)` tuples sorted by frame number.

**Marker absolute/relative mode**: `TrackConfig.markers_absolute` controls whether loaded marker timecodes are taken as-is (`True`) or have the track's start TC added on load (`False`). The offset is baked into `self._markers` at load time; `tc_offset_frames` is never applied to markers.

**Art-Net timecode offset** (`AppConfig.tc_offset_frames`): applied only to the frame number in outgoing Art-Net UDP packets. Never affects the display timecode or `self._markers`. Set via `--tc-offset [±]HH:MM:SS:FF` CLI flag or Settings → Network in the TUI.

**Marker generator** (`marker_gen.py`): uses `fractions.Fraction` throughout — `_FPS_FRAC[29.97] = Fraction(30000, 1001)` for exact NTSC. Frame positions are computed from t=0 and floored once at the end to prevent cumulative drift. `beat_num` counts interval steps within the bar (not denominator-beats), so `skip_beats` is indexed to interval positions (e.g. positions 1–8 for eighth-note intervals in 4/4).

**Marker export offset**: `ExportMarkersModal` accepts `track_start_frames` and offers a "Subtract track start" toggle when non-zero. When enabled, `action_export_markers()` subtracts the track start frame count from each marker before writing, producing relative timecodes.

**Circular import avoidance**: `artnet_timecode.py` lazily imports `tui_app` inside `main()`. `marker_gen.py` imports only from `config.py` and the `timecode` library. `tui_app.py` lazily imports from `artnet_timecode` inside button handlers and action methods.

**CSS is inline**: All Textual CSS lives as `DEFAULT_CSS` strings inside each widget/screen class — there is no separate `.tcss` file.
