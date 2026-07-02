# terminal-tc

Art-Net SMPTE timecode player with a Textual TUI.

Sends ArtTimeCode UDP packets to control lighting consoles, audio systems, or any Art-Net compatible device via SMPTE timecode, with optional audio playback sync.

## Features

- Interactive Textual TUI with play/pause/stop transport controls
- SMPTE timecode formats: 24, 25, 29.97 (drop frame), 30 fps
- Optional audio file sync (WAV, FLAC, OGG, AIFF)
- Multi-track cue list with per-track settings
- Marker file import: Reaper CSV, Audacity labels, CuePoints TSV
- BPM/time-signature marker generator — create bar/beat cue grids in the TUI or CLI
- Marker export to Reaper CSV, with optional track-start offset removal
- Waveform visualisation with Unicode half-block rendering and zoom/pan
- Art-Net timecode output offset (`--tc-offset`) independent of the display timecode
- Stop-on-audio-end — global or per-track
- In-TUI settings screen — no flags needed for basic setup
- Settings persisted at `~/.config/artnet-timecode/config.json`
- Project save/load (local JSON and portable `.tcp` archives)
- Incoming OSC listener for remote transport and track control
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

### Key bindings

| Key | Action |
|---|---|
| `Space` | Play / Pause |
| `S` | Stop (rewind to start TC) |
| `←` / `→` | Scrub −5s / +5s |
| `↑` / `↓` | Previous / next marker |
| `Enter` | Jump to selected marker |
| `W` | Toggle waveform panel |
| `[` / `]` | Zoom in / out on waveform |
| `Shift+←` / `Shift+→` | Pan waveform |
| `0` | Reset zoom to full view |
| `T` | Focus track list |
| `M` | Focus marker list |
| `A` / `E` / `D` | Add / edit / delete track |
| `X` | Export markers to CSV |
| `G` | Open marker generator |
| `Ctrl+,` | Open settings |
| `P` | Open projects |
| `Q` / `Esc` | Quit |

### Common CLI overrides

All settings can be changed in the TUI settings screen. CLI flags override saved config for the current run only.

```bash
artnet-timecode --ip 192.168.1.255 --fps 30
artnet-timecode --fps 25 --start-hours 1 --audio show.wav --markers cues.csv
artnet-timecode --tc-offset +00:01:00:00     # shift Art-Net output forward 1 minute
```

| Flag | Description |
|---|---|
| `--ip` | Art-Net destination IP (or broadcast address) |
| `--port` | Art-Net UDP port (default: 6454) |
| `--broadcast` | Enable broadcast mode |
| `--fps` | Frames per second: 24, 25, 29.97, 30 |
| `--start-hours/minutes/seconds/frames` | Timecode start offset |
| `--tc-offset [±]HH:MM:SS:FF` | Offset applied only to Art-Net output; display is unchanged |
| `--audio` | Audio file to sync playback (WAV, FLAC, OGG, AIFF) |
| `--markers` | Marker/cue file (auto-detected format) |
| `--marker-format` | Force format: auto, reaper, audacity, cuepoints |

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

Markers can be loaded as **absolute** (timecodes taken as-is) or **relative** (track start TC is added to every marker on load). This is configured per-track in the track editor.

## Marker Generator

Press `G` in the TUI to open the marker generator. It builds a bar/beat cue grid from musical parameters and merges the result into the current track's marker list (Append or Replace).

| Field | Description |
|---|---|
| BPM | Tempo (beats per minute) |
| Time signature | e.g. `4/4`, `6/8`, `3/4` |
| Beat unit | Note value next to the tempo marking on the chart, e.g. `1/4` or `1/4.` for compound meters |
| Start / end bar | Inclusive bar range to generate |
| Interval | Spacing between markers: `1bar`, `1/4`, `1/8`, `1/8t`, `0.5bar`, etc. |
| Skip beats | Comma-separated 1-indexed interval positions to omit each bar, e.g. `4,8` |
| Name template | Supports `{bar}` and `{beat}`, e.g. `Bar {bar} Beat {beat}` |
| Anchor TC | Timecode of bar 1 beat 1 of this section (pre-filled from the track start) |
| Mode | **Append** adds to existing markers; **Replace all** clears first |

The generator can be run multiple times with different bar ranges to build composite marker lists across non-consecutive musical sections.

### CLI generation (headless)

Generate a Reaper CSV without launching the TUI:

```bash
artnet-timecode --generate-markers --bpm 120 --time-sig 4/4 --end-bar 32 \
    --interval 1bar --fps 25 --generate-markers-out cues.csv
```

| Flag | Description |
|---|---|
| `--generate-markers` | Enable generation mode (exits after writing) |
| `--bpm BPM` | Tempo (required) |
| `--time-sig N/D` | Time signature (default: `4/4`) |
| `--beat-unit NOTE` | Beat unit for compound meters (default: `1/4`) |
| `--start-bar N` | First bar, 1-indexed (default: `1`) |
| `--end-bar N` | Last bar, inclusive (required) |
| `--interval TOKEN` | Marker spacing (default: `1bar`) |
| `--skip-beats N[,N…]` | Interval positions to omit per bar |
| `--marker-name-template TMPL` | Name template (default: `Bar {bar}`) |
| `--marker-anchor HH:MM:SS:FF` | TC of bar 1 beat 1 (default: `00:00:00:00`) |
| `--generate-markers-out PATH` | Output file; omit to print to stdout |

## Marker Export

Press `X` to export the current track's markers to a Reaper-format CSV.

If the track has a non-zero start timecode (e.g. a 21-hour anchor), the export modal offers a **Subtract track start** toggle. Turning it on removes the track start offset from every exported timecode, producing relative timecodes starting near `00:00:00:00` — useful for re-importing the file as a relative marker list or for documentation independent of the show start.

## Art-Net Timecode Offset

`--tc-offset` (also configurable in Settings → Network) shifts the timecode value sent in Art-Net UDP packets without affecting the on-screen display. Useful when a console expects timecode to start at a different hour than the show file's internal clock.

## OSC Control

The app can receive incoming OSC messages to drive playback and track selection from DAWs, QLab, TouchOSC, or any OSC-capable controller.

**Enable:** open Settings (`Ctrl+,`) → Network tab → flip the **OSC Listener** switch. Set the port (default: `9000`) and save. The listener starts immediately — no restart required. Status is shown in the main info panel.

| Address | Argument | Action |
|---|---|---|
| `/play` | — | Start or resume playback |
| `/pause` | — | Pause playback |
| `/stop` | — | Stop and rewind to start TC |
| `/toggle` | — | Toggle play/pause |
| `/track` | `int` | Switch to track by 0-based index |
| `/track` | `string` | Switch to track by name (case-insensitive) |

Out-of-range indices and unmatched names are silently ignored.

**Quick test** (requires `python-osc`, installed as a dependency):

```bash
python -c "from pythonosc.udp_client import SimpleUDPClient; SimpleUDPClient('127.0.0.1', 9000).send_message('/play', [])"
python -c "from pythonosc.udp_client import SimpleUDPClient; SimpleUDPClient('127.0.0.1', 9000).send_message('/track', 1)"
```

## Configuration

Settings persist automatically at `~/.config/artnet-timecode/config.json`. The in-TUI settings screen covers all common options. CLI flags override the saved config for the current run but do not overwrite it.

## Development

```bash
pip install -e ".[dev]"   # includes pytest
artnet-timecode            # or: python -m terminal_tc
pytest tests/              # run the marker-generator unit tests
```
