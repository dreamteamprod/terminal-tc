# terminal-tc

Art-Net SMPTE timecode player with a Textual TUI.

Sends ArtTimeCode UDP packets to control lighting consoles, audio systems, or any Art-Net compatible device via SMPTE timecode, with optional audio playback sync.

## Features

- Interactive Textual TUI with play/pause/stop transport controls
- SMPTE timecode formats: 24, 25, 29.97 (drop frame), 30 fps
- Optional audio file sync (WAV, FLAC, OGG, AIFF)
- Multi-track cue list with per-track settings
- Marker file import: Reaper CSV, Audacity labels, CuePoints TSV
- Waveform visualisation with Unicode half-block rendering
- In-TUI settings screen — no flags needed for basic setup
- Settings persisted at `~/.config/artnet-timecode/config.json`
- Non-interactive / headless mode (piped stdin or CI)

## Requirements

- Python 3.10+
- **PortAudio** (system library, required for audio playback):
  - macOS: `brew install portaudio`
  - Debian/Ubuntu: `sudo apt install libportaudio2`
  - Windows: bundled with `sounddevice` wheels (usually no action needed)
  - WSL: audio playback is silently disabled if no output devices are detected

## Installation

```bash
git clone https://github.com/timbradgate/terminal-tc.git
cd terminal-tc
pip install -e .
```

Verify the entry point works:

```bash
artnet-timecode --help
```

## Usage

### Launch the TUI

```bash
artnet-timecode
```

### Common CLI overrides

All settings can be changed in the TUI settings screen. CLI flags override saved config for the current run only.

```bash
artnet-timecode --ip 192.168.1.255 --fps 30
artnet-timecode --fps 25 --start-hours 1 --audio show.wav --markers cues.csv
```

| Flag | Description |
|---|---|
| `--ip` | Art-Net destination IP (or broadcast address) |
| `--port` | Art-Net UDP port (default: 6454) |
| `--broadcast` | Enable broadcast mode |
| `--fps` | Frames per second: 24, 25, 29.97, 30 |
| `--start-hours/minutes/seconds/frames` | Timecode start offset |
| `--audio` | Audio file to sync playback (WAV, FLAC, OGG, AIFF) |
| `--markers` | Marker/cue file (auto-detected format) |

### Run as a module

```bash
python -m terminal_tc
```

## Marker File Formats

Three formats are auto-detected by inspecting the first non-empty line:

| Format | Detection | Columns |
|---|---|---|
| **Reaper CSV** | First line starts with `#` | `#,name,time` |
| **CuePoints TSV** | First line starts with `Track` | Tab-separated with header |
| **Audacity labels** | Everything else | `start\tend\tlabel` |

All formats normalise to `(id, name, timecode)` tuples sorted by frame number.

## Configuration

Settings persist automatically at `~/.config/artnet-timecode/config.json`. The in-TUI settings screen covers all common options. CLI flags override the saved config for the current run but do not overwrite it.

## Development

```bash
pip install -e .
artnet-timecode          # or: python -m terminal_tc
```
