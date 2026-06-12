# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
# Install dependencies (once)
pip install -r requirements.txt

# Launch the interactive TUI (reads saved config automatically)
python artnet_timecode.py

# Common CLI overrides
python artnet_timecode.py --ip 192.168.1.255 --fps 30
python artnet_timecode.py --fps 25 --start-hours 1 --audio show.wav --markers cues.csv
```

There are no tests or linting scripts defined in this project.

## Architecture

Three files, each with a single responsibility:

- **`config.py`** — `AppConfig` dataclass, JSON persistence (`~/.config/artnet-timecode/config.json`), and `validate_config`. Imported by both other files.
- **`artnet_timecode.py`** — The headless player engine: Art-Net UDP packet builder, timecode helpers (wrapping the `timecode` library), multi-format marker file parsing, and `ArtNetTimecodePlayer` (threading, audio, transport controls). Also contains `main()`, CLI argument parsing, and the three-layer config merge (defaults → saved JSON → explicit CLI flags). In non-interactive mode (piped stdin) it auto-plays without the TUI.
- **`tui_app.py`** — [Textual](https://textual.textualize.io/) TUI. `TimecodeApp` receives a fully-constructed `ArtNetTimecodePlayer` and a markers list; it never touches networking or audio directly. A 30 Hz poll loop (`set_interval`) updates the display from player state. Includes a `ModalScreen`-based settings screen (in-TUI, zero-flag startup), a command palette (`TimecodeCommands`), `WaveformWidget` (half-block Unicode rendering), and `MarkerList` (scrollable cursor widget).

## Key design details

**Config merge order** (`main()`): CLI flags win over saved JSON, which wins over dataclass defaults. `argparse.SUPPRESS` is used so only explicitly-passed flags override saved config.

**Timecode library quirk**: The `timecode` library uses 1-indexed `frames` constructor argument (`frames=1` → `00:00:00:00`), but its `.frame_number` property is 0-indexed. `make_tc()` and `tc_from_frame_number()` encapsulate this.

**Frame ticker accuracy**: The ticker thread uses absolute time anchoring (`wall_origin + n * interval`) rather than accumulated sleeps, so timing error does not drift over long runs. A busy-spin covers the final millisecond.

**Marker formats**: Three supported formats auto-detected by sniffing the first non-empty line — Reaper CSV (starts with `#`), CuePoints TSV (starts with `Track`), and Audacity labels (everything else). All normalise to `(id, name, Timecode)` tuples sorted by frame number.

**CSS is inline**: All Textual CSS lives as `DEFAULT_CSS` strings inside each widget/screen class — there is no separate `.tcss` file.
