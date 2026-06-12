"""
Textual TUI for the Art-Net Timecode Player.
"""

from __future__ import annotations

import dataclasses
import os
from typing import TYPE_CHECKING

import numpy as np
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Digits,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)
from textual.widget import Widget

from config import AppConfig, TrackConfig, save_config, validate_config, validate_track_config

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

    def set_markers(self, markers: list) -> None:
        self._markers = markers
        self.cursor = 0
        self._scroll = 0
        self.refresh()

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

    def _compute_envelope(self) -> None:
        self._envelope = None
        self._audio_duration_secs = 0.0
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

    def on_mount(self) -> None:
        self._compute_envelope()

    def reload(self, player: "ArtNetTimecodePlayer", markers: list) -> None:
        """Swap the player and markers, recompute waveform envelope, reset playhead."""
        self._player = player
        self._markers = markers
        self._playhead_frac = 0.0
        self._compute_envelope()
        self.refresh()

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


class TrackList(Widget):
    """Scrollable cue list. cursor = nav highlight; active_idx = currently playing."""

    DEFAULT_CSS = """
    TrackList {
        height: 1fr;
        min-height: 5;
        overflow: hidden;
        padding: 0 1;
        border-top: solid $primary;
    }
    TrackList.nav-active {
        border-top: solid $accent;
    }
    """

    cursor: int = reactive(0, repaint=True)
    active_idx: int = reactive(0, repaint=True)

    class Activated(Message):
        """Posted when the user activates a track row."""
        def __init__(self, idx: int) -> None:
            super().__init__()
            self.idx = idx

    def __init__(self, tracks: list, active_idx: int = 0, **kwargs) -> None:
        super().__init__(**kwargs)
        self._tracks = tracks
        self.active_idx = active_idx
        self.cursor = active_idx
        self._scroll = 0

    def render(self) -> Text:
        if not self._tracks:
            return Text("  No tracks", style="dim italic")

        h = max(1, self.size.height - 1)  # reserve header row
        out = Text()
        out.append("  #  NAME                   START TC      ♪  ≡\n", style="bold dim")

        for i in range(self._scroll, min(self._scroll + h, len(self._tracks))):
            t = self._tracks[i]
            is_active = i == self.active_idx
            is_cursor = i == self.cursor

            active_dot = "●" if is_active else " "
            arrow = "▶" if is_cursor else " "
            name = t.name[:21].ljust(21)
            tc_str = f"{t.start_hours:02d}:{t.start_minutes:02d}:{t.start_seconds:02d}:{t.start_frames:02d}"
            audio_icon = "♪" if (t.audio and os.path.isfile(t.audio)) else "—"
            marker_icon = "≡" if (t.markers and os.path.isfile(t.markers)) else "—"

            if is_cursor and is_active:
                style = "bold green"
            elif is_cursor:
                style = "bold"
            elif is_active:
                style = "green"
            else:
                style = "dim"

            out.append(
                f"{active_dot}{arrow} {i + 1:<2} {name}  {tc_str}  {audio_icon}  {marker_icon}\n",
                style=style,
            )
        return out

    def action_cursor_up(self) -> None:
        self.move_cursor(-1)

    def action_cursor_down(self) -> None:
        self.move_cursor(+1)

    def action_activate(self) -> None:
        self.post_message(TrackList.Activated(self.cursor))

    def move_cursor(self, delta: int) -> None:
        if not self._tracks:
            return
        self.cursor = max(0, min(len(self._tracks) - 1, self.cursor + delta))
        self._clamp()
        self.refresh()

    def set_tracks(self, tracks: list, active_idx: int) -> None:
        self._tracks = tracks
        self.active_idx = active_idx
        self.cursor = active_idx
        self._scroll = 0
        self.refresh()

    def _clamp(self) -> None:
        h = max(1, self.size.height - 1)
        if self.cursor < self._scroll:
            self._scroll = self.cursor
        elif self.cursor >= self._scroll + h:
            self._scroll = max(0, self.cursor - h + 1)

    def on_click(self, event) -> None:
        row = event.offset.y - 1 + self._scroll  # -1 for header row
        if 0 <= row < len(self._tracks):
            self.cursor = row
            self._clamp()
            self.post_message(TrackList.Activated(row))


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
        ("Add Track", "Add a new track to the session", "action_add_track"),
        ("Edit Track", "Edit the selected track", "action_edit_track"),
        ("Delete Track", "Delete the selected track", "action_delete_track"),
        ("Settings", "Configure network and timecode", "action_open_settings"),
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
        Binding("t", "focus_tracks", "Tracks"),
        Binding("m", "focus_markers", "Markers"),
        Binding("a", "add_track", "Add Track"),
        Binding("e", "edit_track", "Edit Track"),
        Binding("d", "delete_track", "Del Track"),
        Binding("ctrl+comma", "open_settings", "Settings"),
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

    #track-list {
        min-height: 5;
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
    #marker-panel.nav-active { border: solid $accent; }

    #markers-hdr {
        text-style: bold;
        color: $text 60%;
        height: 2;
    }
    """

    def __init__(
        self,
        config: AppConfig,
        player: "ArtNetTimecodePlayer",
        markers: list | None = None,
        tracks: list | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._config = config
        self._player = player
        self._markers = markers or []
        self._tracks: list = tracks if tracks is not None else list(config.tracks)
        self._active_idx: int = 0
        self._nav_mode: str = ""  # "", "tracks", or "markers"

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
                yield TrackList(self._tracks, self._active_idx, id="track-list")
                with Horizontal(id="transport"):
                    yield Button("▶  Play / Pause", id="btn-play", variant="primary")
                    yield Button("■  Stop", id="btn-stop", variant="error")
            with Vertical(id="marker-panel"):
                yield Label("── MARKERS ────────────────────────", id="markers-hdr")
                yield MarkerList(self._markers, id="markers")
        yield Footer()

    def _active_track(self) -> TrackConfig | None:
        if self._tracks and 0 <= self._active_idx < len(self._tracks):
            return self._tracks[self._active_idx]
        return None

    def _info_text(self) -> str:
        p = self._player
        c = self._config
        track = self._active_track()
        dest = c.ip
        if c.broadcast or c.ip.endswith(".255"):
            dest += "  (broadcast)"
        dest += f":{c.port}"
        fps_label = _FPS_LABEL.get(c.fps, f"{c.fps} fps")
        audio = self._audio_status()
        name = track.name if track else "—"
        return (
            f"Track:        {name}\n"
            f"Destination:  {dest}\n"
            f"Frame rate:   {fps_label}\n"
            f"Start TC:     {p.start_tc}\n"
            f"Audio:        {audio}"
        )

    def _audio_status(self) -> str:
        p = self._player
        track = self._active_track()
        if not track or not track.audio:
            return "—  none"
        if p._audio_error:
            return f"✗  {p._audio_error}"
        if p._audio_loaded:
            dur = p.audio_duration_str()
            return f"✓  {os.path.basename(track.audio)}  ({dur} @ {p._audio_samplerate} Hz)"
        return "Loading…"

    def on_mount(self) -> None:
        if self._markers:
            self.query_one("#marker-panel").add_class("visible")
        self._last_frame: int = -1
        self._last_wave_col: int = -1
        self.set_interval(1 / 30, self._poll)

    def _poll(self) -> None:
        try:
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
                            (tc.frame_number - p.start_tc.frame_number)
                            / duration_frames,
                        ),
                    )
                    W = wf.size.width
                    if W > 0:
                        col = int(frac * (W - 1))
                        if col != self._last_wave_col:
                            wf._playhead_frac = frac
                            self._last_wave_col = col
        except NoMatches:
            pass  # widget tree is mid-recompose; skip this tick

    def on_unmount(self) -> None:
        self._player.stop()
        self._player.shutdown()

    # ── Track management ──────────────────────────────────────────────────────

    def _update_config_tracks(self) -> AppConfig:
        return dataclasses.replace(self._config, tracks=list(self._tracks))

    def _switch_track(self, idx: int) -> None:
        """Stop current player, build player for tracks[idx], refresh widgets in place."""
        if not self._tracks or not (0 <= idx < len(self._tracks)):
            return
        if idx == self._active_idx:
            return

        from artnet_timecode import build_player_from_track, build_markers_from_track

        old_player = self._player
        old_player.stop()
        old_player.shutdown()

        track = self._tracks[idx]
        self._player = build_player_from_track(track, self._config)
        self._markers = build_markers_from_track(track, self._config.fps)
        self._active_idx = idx
        self._last_frame = -1
        self._last_wave_col = -1

        try:
            self.query_one("#waveform", WaveformWidget).reload(self._player, self._markers)
            self.query_one("#markers", MarkerList).set_markers(self._markers)
            self.query_one("#info", Static).update(self._info_text())
            marker_panel = self.query_one("#marker-panel")
            if self._markers:
                marker_panel.add_class("visible")
            else:
                marker_panel.remove_class("visible")
            tl = self.query_one("#track-list", TrackList)
            tl.active_idx = idx
            tl.cursor = idx
            tl.refresh()
        except NoMatches:
            pass

    def on_track_list_activated(self, event: TrackList.Activated) -> None:
        self._set_nav_mode("")
        self._switch_track(event.idx)

    @work
    async def action_add_track(self) -> None:
        new_track: TrackConfig | None = await self.push_screen_wait(
            TrackEditModal(None, self._config.fps)
        )
        if new_track is None:
            return
        self._tracks.append(new_track)
        save_config(self._update_config_tracks())
        self.query_one("#track-list", TrackList).set_tracks(self._tracks, self._active_idx)

    @work
    async def action_edit_track(self) -> None:
        tl = self.query_one("#track-list", TrackList)
        idx = tl.cursor
        updated: TrackConfig | None = await self.push_screen_wait(
            TrackEditModal(self._tracks[idx], self._config.fps)
        )
        if updated is None:
            return
        self._tracks[idx] = updated
        save_config(self._update_config_tracks())
        if idx == self._active_idx:
            # Rebuild active player with updated track config
            saved_idx = self._active_idx
            self._active_idx = -1  # bypass _switch_track's same-index guard
            self._switch_track(saved_idx)
        tl.set_tracks(self._tracks, self._active_idx)

    def action_delete_track(self) -> None:
        if len(self._tracks) <= 1:
            self.notify("Cannot delete the last track", severity="warning")
            return
        tl = self.query_one("#track-list", TrackList)
        idx = tl.cursor
        self._tracks.pop(idx)
        new_active = min(self._active_idx, len(self._tracks) - 1)
        if idx == self._active_idx:
            self._active_idx = -1  # bypass same-index guard so _switch_track runs
            self._switch_track(new_active)
        else:
            if idx < self._active_idx:
                self._active_idx -= 1
            new_active = self._active_idx
        save_config(self._update_config_tracks())
        tl.set_tracks(self._tracks, self._active_idx)

    # ── Settings ──────────────────────────────────────────────────────────────

    @work
    async def action_open_settings(self) -> None:
        result: tuple | None = await self.push_screen_wait(
            SettingsScreen(self._config, self._tracks)
        )
        if result is None:
            return
        new_config, new_tracks = result
        self._player.stop()
        self._player.shutdown()
        self._config = new_config
        self._tracks = new_tracks
        self._active_idx = min(self._active_idx, max(0, len(self._tracks) - 1))
        save_config(self._update_config_tracks())

        from artnet_timecode import build_player_from_track, build_markers_from_track

        self._player = build_player_from_track(self._tracks[self._active_idx], new_config)
        self._markers = build_markers_from_track(self._tracks[self._active_idx], new_config.fps)
        self._last_frame = -1
        self._last_wave_col = -1
        await self.recompose()
        if self._markers:
            self.query_one("#marker-panel").add_class("visible")
        self.query_one("#track-list", TrackList).set_tracks(self._tracks, self._active_idx)

    # ── Transport ─────────────────────────────────────────────────────────────

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

    def _set_nav_mode(self, mode: str) -> None:
        self._nav_mode = mode
        self.query_one("#track-list", TrackList).set_class(mode == "tracks", "nav-active")
        self.query_one("#marker-panel").set_class(mode == "markers", "nav-active")
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("add_track", "edit_track", "delete_track"):
            return True if self._nav_mode == "tracks" else False
        if action in ("prev_marker", "next_marker", "jump_marker"):
            return True if self._nav_mode == "markers" else False
        return True

    def action_focus_tracks(self) -> None:
        self._set_nav_mode("" if self._nav_mode == "tracks" else "tracks")

    def action_focus_markers(self) -> None:
        self._set_nav_mode("" if self._nav_mode == "markers" else "markers")

    def action_prev_marker(self) -> None:
        if self._nav_mode == "tracks":
            self.query_one("#track-list", TrackList).move_cursor(-1)
        elif self._nav_mode == "markers" and self._markers:
            self.query_one("#markers", MarkerList).move_cursor(-1)

    def action_next_marker(self) -> None:
        if self._nav_mode == "tracks":
            self.query_one("#track-list", TrackList).move_cursor(+1)
        elif self._nav_mode == "markers" and self._markers:
            self.query_one("#markers", MarkerList).move_cursor(+1)

    def action_jump_marker(self) -> None:
        if self._nav_mode == "tracks":
            tl = self.query_one("#track-list", TrackList)
            self._set_nav_mode("")
            self._switch_track(tl.cursor)
        elif self._nav_mode == "markers" and self._markers:
            m = self.query_one("#markers", MarkerList).selected_marker()
            if m:
                self._player.seek_to_frame(m[2].frame_number)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-play":
            self._player.toggle_play_pause()
        elif event.button.id == "btn-stop":
            self._player.stop()


# ── Track edit modal ───────────────────────────────────────────────────────────


class TrackEditModal(ModalScreen):
    """Modal for adding or editing a single TrackConfig."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    DEFAULT_CSS = """
    TrackEditModal {
        align: center middle;
    }
    TrackEditModal > Vertical {
        width: 74;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    TrackEditModal #modal-title {
        text-style: bold;
        height: 2;
        padding: 0 0 1 0;
    }
    TrackEditModal .field-row {
        height: 3;
        align: left middle;
        margin: 0 0 1 0;
    }
    TrackEditModal .field-label {
        width: 26;
        height: 3;
        content-align: left middle;
        padding: 1 0;
    }
    TrackEditModal Input { width: 1fr; }
    TrackEditModal Select { width: 1fr; }
    TrackEditModal #validation-error {
        color: $error;
        height: auto;
        padding: 0 0 1 0;
        display: none;
    }
    TrackEditModal #validation-error.visible { display: block; }
    TrackEditModal #modal-buttons {
        height: 3;
        align: right middle;
        margin: 1 0 0 0;
    }
    TrackEditModal #modal-buttons Button {
        margin: 0 0 0 1;
        min-width: 10;
    }
    """

    def __init__(self, track: TrackConfig | None, fps: float) -> None:
        super().__init__()
        self._track = track  # None = new track
        self._fps = fps

    def compose(self) -> ComposeResult:
        t = self._track or TrackConfig()
        title = "Edit Track" if self._track else "Add Track"
        fmt_options = [
            ("Auto-detect", "auto"),
            ("Reaper CSV", "reaper"),
            ("Audacity Labels", "audacity"),
            ("CuePoints TSV", "cuepoints"),
        ]
        with Vertical():
            yield Label(f"♪  {title}", id="modal-title")
            with Horizontal(classes="field-row"):
                yield Label("Track Name", classes="field-label")
                yield Input(value=t.name, id="inp-name", placeholder="Track 1")
            with Horizontal(classes="field-row"):
                yield Label("Start Hours (0–23)", classes="field-label")
                yield Input(value=str(t.start_hours), id="inp-hours", type="integer")
            with Horizontal(classes="field-row"):
                yield Label("Start Minutes (0–59)", classes="field-label")
                yield Input(value=str(t.start_minutes), id="inp-minutes", type="integer")
            with Horizontal(classes="field-row"):
                yield Label("Start Seconds (0–59)", classes="field-label")
                yield Input(value=str(t.start_seconds), id="inp-seconds", type="integer")
            with Horizontal(classes="field-row"):
                yield Label("Start Frames", classes="field-label")
                yield Input(value=str(t.start_frames), id="inp-frames", type="integer")
            with Horizontal(classes="field-row"):
                yield Label("Audio File", classes="field-label")
                yield Input(
                    value=t.audio or "",
                    id="inp-audio",
                    placeholder="path/to/audio.wav",
                )
                yield Button("Browse…", id="btn-audio-browse")
            with Horizontal(classes="field-row"):
                yield Label("Markers File", classes="field-label")
                yield Input(
                    value=t.markers or "",
                    id="inp-markers",
                    placeholder="path/to/markers.csv",
                )
                yield Button("Browse…", id="btn-markers-browse")
            with Horizontal(classes="field-row"):
                yield Label("Marker Format", classes="field-label")
                yield Select(
                    fmt_options,
                    value=t.marker_format,
                    id="sel-marker-format",
                    allow_blank=False,
                )
            yield Static("", id="validation-error")
            with Horizontal(id="modal-buttons"):
                yield Button("Save", id="btn-save", variant="primary")
                yield Button("Cancel", id="btn-cancel")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button.id
        if btn == "btn-cancel":
            self.dismiss(None)
        elif btn == "btn-save":
            self._try_save()
        elif btn == "btn-audio-browse":
            start = self.query_one("#inp-audio", Input).value or os.path.expanduser("~")
            self.app.push_screen(FileBrowserModal(start), self._on_audio_chosen)
        elif btn == "btn-markers-browse":
            start = self.query_one("#inp-markers", Input).value or os.path.expanduser("~")
            self.app.push_screen(FileBrowserModal(start), self._on_markers_chosen)

    def _on_audio_chosen(self, path: str | None) -> None:
        if path:
            self.query_one("#inp-audio", Input).value = path

    def _on_markers_chosen(self, path: str | None) -> None:
        if path:
            self.query_one("#inp-markers", Input).value = path

    def _try_save(self) -> None:
        try:
            fmt_raw = self.query_one("#sel-marker-format", Select).value
            track = TrackConfig(
                name=self.query_one("#inp-name", Input).value.strip(),
                start_hours=int(self.query_one("#inp-hours", Input).value or "0"),
                start_minutes=int(self.query_one("#inp-minutes", Input).value or "0"),
                start_seconds=int(self.query_one("#inp-seconds", Input).value or "0"),
                start_frames=int(self.query_one("#inp-frames", Input).value or "0"),
                audio=self.query_one("#inp-audio", Input).value.strip() or None,
                markers=self.query_one("#inp-markers", Input).value.strip() or None,
                marker_format=str(fmt_raw) if fmt_raw is not Select.BLANK else "auto",
            )
        except (ValueError, TypeError) as exc:
            self._show_error(f"Invalid value: {exc}")
            return

        errs = validate_track_config(track, self._fps)
        if errs:
            self._show_error("\n".join(f"✗  {e}" for e in errs))
            return

        self.dismiss(track)

    def _show_error(self, msg: str) -> None:
        err = self.query_one("#validation-error", Static)
        err.update(msg)
        err.add_class("visible")


# ── Settings modal ─────────────────────────────────────────────────────────────


class SettingsScreen(ModalScreen):
    """Full-screen modal for configuring network, timecode, and tracks."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    DEFAULT_CSS = """
    SettingsScreen {
        align: center middle;
    }
    SettingsScreen > Vertical {
        width: 74;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    SettingsScreen #settings-title {
        text-style: bold;
        height: 2;
        padding: 0 0 1 0;
    }
    SettingsScreen .field-row {
        height: 3;
        align: left middle;
        margin: 0 0 1 0;
    }
    SettingsScreen .field-label {
        width: 26;
        height: 3;
        content-align: left middle;
        padding: 1 0;
    }
    SettingsScreen Input { width: 1fr; }
    SettingsScreen Select { width: 1fr; }
    SettingsScreen Switch {
        height: 3;
        align: left middle;
    }
    SettingsScreen #validation-error {
        color: $error;
        height: auto;
        padding: 0 0 1 0;
        display: none;
    }
    SettingsScreen #validation-error.visible { display: block; }
    SettingsScreen #settings-buttons {
        height: 3;
        align: right middle;
        margin: 1 0 0 0;
    }
    SettingsScreen #settings-buttons Button {
        margin: 0 0 0 1;
        min-width: 10;
    }
    SettingsScreen #tab-tracks TrackList {
        height: 8;
        min-height: 4;
        border: solid $primary;
    }
    SettingsScreen #track-action-buttons {
        height: 3;
        align: left middle;
        margin: 1 0 0 0;
    }
    SettingsScreen #track-action-buttons Button {
        margin: 0 1 0 0;
        min-width: 14;
    }
    """

    def __init__(self, config: AppConfig, tracks: list) -> None:
        super().__init__()
        self._initial_config = config
        self._working_tracks: list = list(tracks)

    def compose(self) -> ComposeResult:
        cfg = self._initial_config
        fps_options = [(label, float(fps)) for fps, label in _FPS_LABEL.items()]
        with Vertical():
            yield Label("⚙  Settings", id="settings-title")
            with TabbedContent():
                with TabPane("Network", id="tab-network"):
                    with Horizontal(classes="field-row"):
                        yield Label("Destination IP", classes="field-label")
                        yield Input(
                            value=cfg.ip,
                            id="inp-ip",
                            placeholder="e.g. 192.168.1.255",
                        )
                    with Horizontal(classes="field-row"):
                        yield Label("UDP Port", classes="field-label")
                        yield Input(
                            value=str(cfg.port),
                            id="inp-port",
                            type="integer",
                            placeholder="6454",
                        )
                    with Horizontal(classes="field-row"):
                        yield Label("Force Broadcast", classes="field-label")
                        yield Switch(value=cfg.broadcast, id="sw-broadcast")
                with TabPane("Timecode", id="tab-timecode"):
                    with Horizontal(classes="field-row"):
                        yield Label("Frame Rate", classes="field-label")
                        yield Select(
                            fps_options,
                            value=float(cfg.fps),
                            id="sel-fps",
                            allow_blank=False,
                        )
                with TabPane("Tracks", id="tab-tracks"):
                    yield TrackList(
                        self._working_tracks,
                        0,
                        id="settings-track-list",
                    )
                    with Horizontal(id="track-action-buttons"):
                        yield Button("Add Track", id="btn-track-add")
                        yield Button("Edit Track", id="btn-track-edit")
                        yield Button("Delete Track", id="btn-track-delete")
            yield Static("", id="validation-error")
            with Horizontal(id="settings-buttons"):
                yield Button("Save", id="btn-save", variant="primary")
                yield Button("Cancel", id="btn-cancel")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button.id
        if btn == "btn-cancel":
            self.dismiss(None)
        elif btn == "btn-save":
            self._try_save()
        elif btn == "btn-track-add":
            self._open_track_edit(None)
        elif btn == "btn-track-edit":
            tl = self.query_one("#settings-track-list", TrackList)
            self._open_track_edit(tl.cursor)
        elif btn == "btn-track-delete":
            self._delete_track()

    @work
    async def _open_track_edit(self, idx: int | None) -> None:
        existing = self._working_tracks[idx] if idx is not None else None
        result: TrackConfig | None = await self.app.push_screen_wait(
            TrackEditModal(existing, self._initial_config.fps)
        )
        if result is None:
            return
        if idx is None:
            self._working_tracks.append(result)
        else:
            self._working_tracks[idx] = result
        tl = self.query_one("#settings-track-list", TrackList)
        tl.set_tracks(self._working_tracks, tl.active_idx)

    def _delete_track(self) -> None:
        if len(self._working_tracks) <= 1:
            self.query_one("#validation-error", Static).update("Cannot delete the last track")
            self.query_one("#validation-error", Static).add_class("visible")
            return
        tl = self.query_one("#settings-track-list", TrackList)
        idx = tl.cursor
        self._working_tracks.pop(idx)
        new_active = min(tl.active_idx, len(self._working_tracks) - 1)
        tl.set_tracks(self._working_tracks, new_active)

    def _try_save(self) -> None:
        try:
            fps_raw = self.query_one("#sel-fps", Select).value
            port_str = self.query_one("#inp-port", Input).value.strip()
            cfg = dataclasses.replace(
                self._initial_config,
                ip=self.query_one("#inp-ip", Input).value.strip(),
                port=int(port_str) if port_str else 6454,
                broadcast=self.query_one("#sw-broadcast", Switch).value,
                fps=float(fps_raw) if fps_raw is not Select.BLANK else 25.0,
            )
        except (ValueError, TypeError) as exc:
            self._show_error(f"Invalid value: {exc}")
            return

        fps_errs = []
        from config import SUPPORTED_FPS
        if cfg.fps not in SUPPORTED_FPS:
            fps_errs.append(f"Frame rate must be one of {SUPPORTED_FPS}")
        if fps_errs:
            self._show_error("\n".join(f"✗  {e}" for e in fps_errs))
            return

        self.dismiss((cfg, list(self._working_tracks)))

    def _show_error(self, msg: str) -> None:
        err = self.query_one("#validation-error", Static)
        err.update(msg)
        err.add_class("visible")


# ── File browser modal ─────────────────────────────────────────────────────────


class FileBrowserModal(ModalScreen):
    """Directory tree browser; selecting a file dismisses with its path."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    DEFAULT_CSS = """
    FileBrowserModal {
        align: center middle;
    }
    FileBrowserModal > Vertical {
        width: 80;
        height: 36;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    FileBrowserModal DirectoryTree {
        height: 1fr;
        margin: 0 0 1 0;
    }
    FileBrowserModal #browser-buttons {
        height: 3;
        align: right middle;
    }
    FileBrowserModal #browser-buttons Button {
        margin: 0 0 0 1;
        min-width: 10;
    }
    """

    def __init__(self, initial_path: str) -> None:
        super().__init__()
        if os.path.isfile(initial_path):
            self._initial_dir = os.path.dirname(os.path.abspath(initial_path))
        elif os.path.isdir(initial_path):
            self._initial_dir = os.path.abspath(initial_path)
        else:
            self._initial_dir = os.path.expanduser("~")

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Select a file:")
            yield DirectoryTree(self._initial_dir, id="dir-tree")
            with Horizontal(id="browser-buttons"):
                yield Button("Cancel", id="btn-cancel")

    def on_directory_tree_file_selected(
        self, event: DirectoryTree.FileSelected
    ) -> None:
        self.dismiss(str(event.path))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
