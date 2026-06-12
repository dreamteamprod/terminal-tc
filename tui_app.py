"""
Textual TUI for the Art-Net Timecode Player.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import numpy as np
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Button, Digits, Footer, Header, Label, Static
from textual.widget import Widget

if TYPE_CHECKING:
    from artnet_timecode import ArtNetTimecodePlayer

_BLOCKS = " ▁▂▃▄▅▆▇█"

_STATE_CLASS = {0: "stopped", 1: "playing", 2: "paused"}
_STATE_LABEL = {0: "■  STOPPED", 1: "▶  PLAYING", 2: "⏸  PAUSED"}
_FPS_LABEL = {
    24: "24 fps  (Film / DCI)",
    25: "25 fps  (EBU / PAL)",
    29.97: "29.97 fps  (Drop Frame / NTSC)",
    30: "30 fps  (SMPTE / HD)",
}


class StateDisplay(Static):
    """Shows the current player state, colour-coded."""

    DEFAULT_CSS = """
    StateDisplay {
        height: 3;
        content-align: center middle;
        text-style: bold;
        width: 100%;
    }
    StateDisplay.stopped { color: $error; }
    StateDisplay.playing { color: $success; }
    StateDisplay.paused  { color: $warning; }
    """

    def update_state(self, state_int: int) -> None:
        new_cls = _STATE_CLASS[state_int]
        for c in _STATE_CLASS.values():
            self.remove_class(c)
        self.add_class(new_cls)
        self.update(_STATE_LABEL[state_int])


class TimecodeDisplay(Digits):
    """Large-digit timecode display, colour-coded by player state."""

    DEFAULT_CSS = """
    TimecodeDisplay {
        height: 5;
        width: 100%;
        content-align: center middle;
        padding: 1;
    }
    TimecodeDisplay.stopped { color: $error; }
    TimecodeDisplay.playing { color: $success; }
    TimecodeDisplay.paused  { color: $warning; }
    """

    def update_tc(self, tc_str: str, state_int: int) -> None:
        new_cls = _STATE_CLASS[state_int]
        for c in _STATE_CLASS.values():
            self.remove_class(c)
        self.add_class(new_cls)
        self.update(tc_str.replace(";", ":"))


class MarkerList(Widget):
    """Scrollable list of cue markers with cursor navigation and mouse-click support."""

    DEFAULT_CSS = """
    MarkerList {
        height: 1fr;
        overflow: hidden;
        padding: 0 1;
    }
    """

    cursor: int = reactive(0, repaint=True)

    def __init__(self, markers: list, **kwargs) -> None:
        super().__init__(**kwargs)
        self._markers = markers
        self._scroll = 0

    def render(self) -> Text:
        if not self._markers:
            return Text("No markers loaded", style="dim italic")
        h = max(1, self.size.height)
        lines: list[Text] = []
        for i in range(self._scroll, min(self._scroll + h, len(self._markers))):
            mid, name, tc = self._markers[i]
            sel = i == self.cursor
            arrow = "▶ " if sel else "  "
            style = "bold green" if sel else "dim"
            lines.append(
                Text(f"{arrow}{mid[:4]:<4}  {name[:20]:<20}  {tc}", style=style)
            )
        while len(lines) < h:
            lines.append(Text(""))
        out = Text()
        for i, ln in enumerate(lines):
            if i:
                out.append("\n")
            out.append_text(ln)
        return out

    def move_cursor(self, delta: int) -> None:
        if not self._markers:
            return
        self.cursor = max(0, min(len(self._markers) - 1, self.cursor + delta))
        self._clamp()
        self.refresh()

    def set_cursor(self, idx: int) -> None:
        if not self._markers:
            return
        self.cursor = max(0, min(len(self._markers) - 1, idx))
        self._clamp()
        self.refresh()

    def auto_track(self, frame_number: int) -> None:
        if not self._markers:
            return
        new = 0
        for i, (_, _, m_tc) in enumerate(self._markers):
            if m_tc.frame_number <= frame_number:
                new = i
            else:
                break
        if new != self.cursor:
            self.cursor = new
            self._clamp()

    def selected_marker(self):
        if self._markers and 0 <= self.cursor < len(self._markers):
            return self._markers[self.cursor]
        return None

    def _clamp(self) -> None:
        h = max(1, self.size.height)
        if self.cursor < self._scroll:
            self._scroll = self.cursor
        elif self.cursor >= self._scroll + h:
            self._scroll = max(0, self.cursor - h + 1)

    def on_click(self, event) -> None:
        row = event.offset.y + self._scroll
        if 0 <= row < len(self._markers):
            self.set_cursor(row)


class WaveformWidget(Widget):
    """Audio waveform with a scrolling playhead and marker lines."""

    DEFAULT_CSS = """
    WaveformWidget {
        height: 10;
        width: 100%;
        margin: 0 0 1 0;
    }
    """

    _playhead_frac: reactive[float] = reactive(0.0, repaint=True)

    def __init__(self, player: "ArtNetTimecodePlayer", markers: list, **kwargs) -> None:
        super().__init__(**kwargs)
        self._player = player
        self._markers = markers
        self._envelope: "np.ndarray | None" = None
        self._audio_duration_secs: float = 0.0

    def on_mount(self) -> None:
        p = self._player
        if not p._audio_loaded or p._audio_data is None:
            return
        mono = np.abs(p._audio_data.mean(axis=1))
        N = len(mono)
        HIRES = 2000
        trim = (N // HIRES) * HIRES
        envelope = mono[:trim].reshape(HIRES, -1).max(axis=1) if trim > 0 else mono
        peak = float(envelope.max())
        self._envelope = (
            (envelope / peak).astype(np.float32)
            if peak > 0
            else envelope.astype(np.float32)
        )
        self._audio_duration_secs = N / p._audio_samplerate

    def render(self) -> Text:
        if self._envelope is None:
            return Text("  No audio loaded", style="dim italic")

        W = max(1, self.size.width)
        H = max(1, self.size.height)

        env = self._envelope
        n = len(env)
        if n >= W:
            starts = np.arange(W) * n // W
            cols = np.maximum.reduceat(env, starts)[:W]
        else:
            cols = np.interp(np.arange(W), np.linspace(0, W - 1, n), env).astype(
                np.float32
            )

        ph_col = int(self._playhead_frac * (W - 1))

        marker_cols: set[int] = set()
        if self._audio_duration_secs > 0:
            p = self._player
            start_fn = p.start_tc.frame_number
            total_frames = self._audio_duration_secs * p.fps
            for _, _, tc in self._markers:
                frac = (tc.frame_number - start_fn) / total_frames
                if 0.0 <= frac <= 1.0:
                    marker_cols.add(int(frac * (W - 1)))

        # Half-block rendering: each terminal row = 2 vertical "half-rows",
        # giving 2x vertical resolution using ▀ / ▄ / █.
        center_hr = H - 0.5  # centre of the widget in half-row units
        fill_radii = cols * center_hr  # per-column fill radius (broadcast-ready)

        out = Text()
        for row in range(H):
            if row > 0:
                out.append("\n")
            upper_dist = abs(2 * row - center_hr)
            lower_dist = abs(2 * row + 1 - center_hr)
            upper_filled = upper_dist <= fill_radii  # shape (W,) bool
            lower_filled = lower_dist <= fill_radii

            for x in range(W):
                u = bool(upper_filled[x])
                lo = bool(lower_filled[x])
                char = "█" if (u and lo) else ("▀" if u else ("▄" if lo else " "))

                if x == ph_col:
                    out.append("│", style="bold bright_white")
                elif x in marker_cols:
                    out.append("│", style="bold bright_yellow")
                elif u or lo:
                    out.append(char, style="green dim")
                else:
                    out.append(" ")
        return out


class TimecodeCommands(Provider):
    """Command palette entries for all player actions."""

    _COMMANDS = [
        ("Play / Pause", "Toggle playback", "action_toggle_play"),
        ("Stop", "Stop playback and reset", "action_stop"),
        ("Scrub Forward", "Jump forward 5 seconds", "action_scrub_fwd"),
        ("Scrub Back", "Jump back 5 seconds", "action_scrub_back"),
        ("Previous Marker", "Go to previous cue marker", "action_prev_marker"),
        ("Next Marker", "Go to next cue marker", "action_next_marker"),
        ("Jump to Marker", "Seek to selected marker", "action_jump_marker"),
        ("Toggle Waveform", "Show or hide waveform", "action_toggle_waveform"),
        ("Quit", "Exit the application", "action_quit"),
    ]

    async def discover(self) -> Hits:
        for name, help_text, action in self._COMMANDS:
            yield Hit(1.0, name, getattr(self.app, action), help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, action in self._COMMANDS:
            score = matcher.match(name)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(name),
                    getattr(self.app, action),
                    text=name,
                    help=help_text,
                )


class TimecodeApp(App[None]):
    """Art-Net Timecode Player — Textual interface."""

    TITLE = "Art-Net Timecode Player"
    COMMANDS = App.COMMANDS | {TimecodeCommands}

    BINDINGS = [
        Binding("space", "toggle_play", "Play/Pause", priority=True),
        Binding("s", "stop", "Stop", priority=True),
        Binding("right", "scrub_fwd", "Scrub +5s", priority=True),
        Binding("left", "scrub_back", "Scrub -5s", priority=True),
        Binding("up", "prev_marker", "Prev marker"),
        Binding("down", "next_marker", "Next marker"),
        Binding("enter", "jump_marker", "Jump", priority=True),
        Binding("w", "toggle_waveform", "Waveform"),
        Binding("q", "quit", "Quit", priority=True),
        Binding("escape", "quit", "Quit", show=False),
    ]

    DEFAULT_CSS = """
    Screen { background: $surface; }

    #layout { height: 1fr; }

    #main-panel {
        width: 65%;
        padding: 1 2;
        border: solid $primary;
    }

    #info {
        padding: 1 0;
        color: $text 70%;
        height: auto;
    }

    #packet-count {
        color: $text 70%;
        height: 1;
        padding: 0 0 1 0;
    }

    #transport {
        height: 3;
        align: center middle;
        margin: 1 0;
    }

    Button { margin: 0 1; min-width: 18; }

    #marker-panel {
        width: 35%;
        padding: 1 1;
        border: solid $primary;
        display: none;
    }

    #marker-panel.visible { display: block; }

    #markers-hdr {
        text-style: bold;
        color: $text 60%;
        height: 2;
    }
    """

    def __init__(
        self, player: "ArtNetTimecodePlayer", args, markers: list = [], **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self._player = player
        self._args = args
        self._markers = markers

    def compose(self) -> ComposeResult:
        p = self._player
        yield Header()
        with Horizontal(id="layout"):
            with Vertical(id="main-panel"):
                yield StateDisplay("■  STOPPED", id="state", classes="stopped")
                yield TimecodeDisplay(
                    str(p.start_tc).replace(";", ":"),
                    id="timecode",
                    classes="stopped",
                )
                yield WaveformWidget(self._player, self._markers, id="waveform")
                yield Static(self._info_text(), id="info")
                yield Static("Packets sent:  0", id="packet-count")
                with Horizontal(id="transport"):
                    yield Button("▶  Play / Pause", id="btn-play", variant="primary")
                    yield Button("■  Stop", id="btn-stop", variant="error")
            with Vertical(id="marker-panel"):
                yield Label("── MARKERS ────────────────────────", id="markers-hdr")
                yield MarkerList(self._markers, id="markers")
        yield Footer()

    def _info_text(self) -> str:
        p = self._player
        a = self._args
        dest = a.ip
        if a.broadcast or a.ip.endswith(".255"):
            dest += "  (broadcast)"
        dest += f":{a.port}"
        fps_label = _FPS_LABEL.get(p.fps, f"{p.fps} fps")
        audio = self._audio_status()
        return (
            f"Destination:  {dest}\n"
            f"Frame rate:   {fps_label}\n"
            f"Start TC:     {p.start_tc}\n"
            f"Audio:        {audio}"
        )

    def _audio_status(self) -> str:
        p = self._player
        a = self._args
        if not a.audio:
            return "—  none"
        if p._audio_error:
            return f"✗  {p._audio_error}"
        if p._audio_loaded:
            dur = p.audio_duration_str()
            return f"✓  {os.path.basename(a.audio)}  ({dur} @ {p._audio_samplerate} Hz)"
        return "Loading…"

    def on_mount(self) -> None:
        if self._markers:
            self.query_one("#marker-panel").add_class("visible")
        self._last_frame: int = -1
        self._last_wave_col: int = -1
        self.set_interval(1 / 30, self._poll)

    def _poll(self) -> None:
        p = self._player
        state_int = int(p.state)
        tc = p.get_tc()
        self.query_one("#state", StateDisplay).update_state(state_int)
        self.query_one("#timecode", TimecodeDisplay).update_tc(str(tc), state_int)
        self.query_one("#packet-count", Static).update(
            f"Packets sent:  {p.packet_count:,}    {p.status_msg}"
        )
        if self._markers and tc.frame_number != self._last_frame:
            self.query_one("#markers", MarkerList).auto_track(tc.frame_number)
        self._last_frame = tc.frame_number

        wf = self.query_one("#waveform", WaveformWidget)
        if wf._envelope is not None:
            duration_frames = wf._audio_duration_secs * p.fps
            if duration_frames > 0:
                frac = max(
                    0.0,
                    min(
                        1.0,
                        (tc.frame_number - p.start_tc.frame_number) / duration_frames,
                    ),
                )
                W = wf.size.width
                if W > 0:
                    col = int(frac * (W - 1))
                    if col != self._last_wave_col:
                        wf._playhead_frac = frac
                        self._last_wave_col = col

    def on_unmount(self) -> None:
        self._player.stop()
        self._player.shutdown()

    def action_toggle_waveform(self) -> None:
        wf = self.query_one("#waveform", WaveformWidget)
        wf.display = not wf.display

    def action_toggle_play(self) -> None:
        self._player.toggle_play_pause()

    def action_stop(self) -> None:
        self._player.stop()

    def action_scrub_fwd(self) -> None:
        self._player.scrub(+5.0)

    def action_scrub_back(self) -> None:
        self._player.scrub(-5.0)

    def action_prev_marker(self) -> None:
        if self._markers:
            self.query_one("#markers", MarkerList).move_cursor(-1)

    def action_next_marker(self) -> None:
        if self._markers:
            self.query_one("#markers", MarkerList).move_cursor(+1)

    def action_jump_marker(self) -> None:
        if not self._markers:
            return
        m = self.query_one("#markers", MarkerList).selected_marker()
        if m:
            self._player.seek_to_frame(m[2].frame_number)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-play":
            self._player.toggle_play_pause()
        elif event.button.id == "btn-stop":
            self._player.stop()
