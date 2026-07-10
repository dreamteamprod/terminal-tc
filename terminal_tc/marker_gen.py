"""Generate cue markers from musical parameters (BPM, time signature, bar range)."""

from __future__ import annotations

from fractions import Fraction

from timecode import Timecode as _LibTimecode

from .config import SUPPORTED_FPS

# Exact fractional frame rates. 29.97 = 30000/1001 (NTSC drop-frame).
_FPS_FRAC: dict[float, Fraction] = {
    24: Fraction(24),
    25: Fraction(25),
    29.97: Fraction(30000, 1001),
    30: Fraction(30),
}

_FPS_STR: dict[float, str] = {
    24: "24",
    25: "25",
    29.97: "29.97",
    30: "30",
}


def _tc_from_frame_number(fps: float, frame_number: int) -> _LibTimecode:
    return _LibTimecode(_FPS_STR[fps], frames=frame_number + 1)


def parse_time_signature(s: str) -> tuple[int, int]:
    """Parse 'N/D' (e.g. '4/4', '6/8') -> (numerator, denominator).

    Denominator must be a power of 2: 1, 2, 4, 8, 16, or 32.
    """
    parts = s.strip().split("/")
    if len(parts) != 2:
        raise ValueError(f"expected N/D format, got {s!r}")
    try:
        num, den = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"non-integer in time signature {s!r}")
    if num < 1:
        raise ValueError(f"numerator must be ≥ 1, got {num}")
    if den not in (1, 2, 4, 8, 16, 32):
        raise ValueError(f"denominator must be a power of 2 (1–32), got {den}")
    return (num, den)


def parse_note_value(token: str) -> Fraction:
    """Parse a note-duration token -> fraction of a whole note.

    Base tokens: "1", "1/2", "1/4", "1/8", "1/16", "1/32".
    Trailing '.' = dotted (×3/2).  Trailing 't' = triplet (×2/3).
    """
    token = token.strip()
    dotted = token.endswith(".")
    triplet = token.endswith("t")
    if dotted:
        token = token[:-1]
    elif triplet:
        token = token[:-1]

    if "/" in token:
        parts = token.split("/")
        if len(parts) != 2:
            raise ValueError(f"expected N/D note value, got {token!r}")
        try:
            value = Fraction(int(parts[0]), int(parts[1]))
        except ValueError:
            raise ValueError(f"non-integer in note value {token!r}")
    else:
        try:
            value = Fraction(int(token))
        except ValueError:
            raise ValueError(f"cannot parse note value {token!r}")

    if dotted:
        value *= Fraction(3, 2)
    elif triplet:
        value *= Fraction(2, 3)

    return value


def parse_interval(token: str, time_sig: tuple[int, int]) -> Fraction:
    """Return interval duration as a fraction of a whole note.

    Handles note value tokens ("1/4", "1/8t", "1/4."), "Nbar" (N may be a
    decimal, e.g. "0.5bar"), and "Nbeat" / "beat" (one denominator-beat).
    """
    token = token.strip()

    if token.endswith("bar"):
        n_str = token[:-3].strip() or "1"
        try:
            n = Fraction(n_str)
        except ValueError:
            raise ValueError(f"cannot parse bar count in {token!r}")
        return n * bar_duration_whole_notes(time_sig)

    if token.endswith("beat"):
        n_str = token[:-4].strip() or "1"
        try:
            n = Fraction(n_str)
        except ValueError:
            raise ValueError(f"cannot parse beat count in {token!r}")
        return n * Fraction(1, time_sig[1])

    return parse_note_value(token)


def bar_duration_whole_notes(time_sig: tuple[int, int]) -> Fraction:
    """Bar duration as a fraction of a whole note: numerator × (1/denominator)."""
    return Fraction(time_sig[0], time_sig[1])


def seconds_per_beat_unit(bpm: float) -> Fraction:
    """Exact seconds per beat_unit as a Fraction. bpm is calibrated to whatever
    note value the chart's tempo marking uses — no quarter-note assumption here.
    """
    return Fraction(60) / Fraction(bpm).limit_denominator(100_000)


def parse_bar_spec(spec: str) -> list[tuple[int, int]]:
    """Parse a comma-separated bar spec into inclusive (start_bar, end_bar) tuples.

    Grammar: spec := token ("," token)* ; token := "N" | "N-M" (N, M ≥ 1).
    "N" becomes (N, N). Tokens are returned in the order given (not sorted or deduped).
    """
    tokens = [t.strip() for t in spec.strip().split(",")]
    if not spec.strip() or any(not t for t in tokens):
        raise ValueError(f"invalid bar spec {spec!r}: empty token")

    ranges: list[tuple[int, int]] = []
    for tok in tokens:
        if "-" in tok:
            parts = tok.split("-")
            if len(parts) != 2:
                raise ValueError(f"invalid bar range {tok!r}: expected 'N-M'")
            try:
                start, end = int(parts[0].strip()), int(parts[1].strip())
            except ValueError:
                raise ValueError(f"non-integer bar range {tok!r}")
            if start < 1:
                raise ValueError(f"bar numbers must be ≥ 1, got {start}")
            if end < start:
                raise ValueError(f"range end must be ≥ start in {tok!r}")
            ranges.append((start, end))
        else:
            try:
                n = int(tok)
            except ValueError:
                raise ValueError(f"non-integer bar {tok!r}")
            if n < 1:
                raise ValueError(f"bar numbers must be ≥ 1, got {n}")
            ranges.append((n, n))
    return ranges


def generate_markers(
    bpm: float,
    time_sig: str,
    start_bar: int,
    end_bar: int,
    interval: str,
    fps: float,
    beat_unit: str = "1/4",
    anchor_frames: int = 0,
    name_template: str = "Bar {bar}",
    skip_beats: list[int] | None = None,
) -> list[tuple[str, str, _LibTimecode]]:
    """Generate (id, name, Timecode) tuples on the same contract as load_markers().

    Frame positions are computed from t=0 using exact Fraction arithmetic and
    floored once at the end — never accumulated — to avoid cumulative drift.

    anchor_frames: absolute frame number where bar 1 beat 1 of this section
    falls in the show.  Default 0 = start of show.  Pass a non-zero value to
    generate markers for a mid-show section without renumbering from bar 1.

    skip_beats: 1-indexed interval-position numbers to omit each bar.  The count
    resets to 1 at the start of each bar and increments by one interval step.
    e.g. with interval="1/8" in 4/4, there are 8 positions per bar; [4,8] drops
    the 4th and 8th eighth notes, giving positions 1,2,3,5,6,7.
    {beat} in name_template also uses these interval positions.
    """
    ts = parse_time_signature(time_sig)
    bar_dur = bar_duration_whole_notes(ts)
    interval_dur = parse_interval(interval, ts)
    beat_unit_dur = parse_note_value(beat_unit)
    spbu = seconds_per_beat_unit(bpm)
    fps_frac = _FPS_FRAC[fps]

    # frames per whole note: (1/beat_unit) beat_units × spbu s/beat_unit × fps
    frames_per_wn = (Fraction(1) / beat_unit_dur) * spbu * fps_frac

    need_beat = "{beat}" in name_template or bool(skip_beats)

    markers: list[tuple[str, str, _LibTimecode]] = []
    start_wn = Fraction(start_bar - 1) * bar_dur  # whole notes from bar 1 to start_bar
    end_wn = Fraction(end_bar) * bar_dur  # exclusive upper bound

    n = 0
    while True:
        pos = start_wn + Fraction(n) * interval_dur
        if pos >= end_wn:
            break

        bar_num = int(pos / bar_dur) + 1  # 1-indexed from song bar 1

        if need_beat:
            pos_in_bar = pos - Fraction(bar_num - 1) * bar_dur
            # beat_num counts interval steps from the start of the bar (1-indexed)
            beat_num = int(pos_in_bar / interval_dur) + 1
            if skip_beats and beat_num in skip_beats:
                n += 1
                continue
            name = name_template.format(bar=bar_num, beat=beat_num)
        else:
            beat_num = None
            name = name_template.format(bar=bar_num)

        exact = pos * frames_per_wn
        frame_offset = exact.numerator // exact.denominator  # floor, once
        absolute_frame = anchor_frames + frame_offset

        tc = _tc_from_frame_number(fps, absolute_frame)
        markers.append((f"{len(markers) + 1:04d}", name, tc))
        n += 1

    return markers


def generate_markers_from_bar_spec(
    bpm: float,
    time_sig: str,
    bar_spec: str,
    interval: str,
    fps: float,
    beat_unit: str = "1/4",
    anchor_frames: int = 0,
    name_template: str = "Bar {bar}",
    skip_beats: list[int] | None = None,
) -> list[tuple[str, str, _LibTimecode]]:
    """Generate markers across one or more bar ranges parsed from bar_spec.

    Calls generate_markers() once per (start_bar, end_bar) sub-range parsed by
    parse_bar_spec(), reusing its exact-Fraction arithmetic unchanged. Results
    are concatenated, sorted by frame number, and ids renumbered 0001.. —
    overlapping ranges are not deduplicated.
    """
    ranges = parse_bar_spec(bar_spec)
    all_markers: list[tuple[str, str, _LibTimecode]] = []
    for start_bar, end_bar in ranges:
        all_markers.extend(
            generate_markers(
                bpm=bpm,
                time_sig=time_sig,
                start_bar=start_bar,
                end_bar=end_bar,
                interval=interval,
                fps=fps,
                beat_unit=beat_unit,
                anchor_frames=anchor_frames,
                name_template=name_template,
                skip_beats=skip_beats,
            )
        )
    all_markers.sort(key=lambda m: m[2].frame_number)
    return [
        (f"{i + 1:04d}", name, tc) for i, (_, name, tc) in enumerate(all_markers)
    ]


def _validate_common_params(
    bpm: float,
    time_sig: str,
    interval: str,
    fps: float,
    beat_unit: str = "1/4",
) -> list[str]:
    """Shared bpm/fps/time_sig/beat_unit/interval checks used by both validators."""
    errs: list[str] = []

    if not isinstance(bpm, (int, float)) or bpm <= 0:
        errs.append("BPM must be a positive number")

    if fps not in SUPPORTED_FPS:
        errs.append(f"Frame rate must be one of {SUPPORTED_FPS}")

    try:
        parse_time_signature(time_sig)
    except ValueError as exc:
        errs.append(f"Invalid time signature: {exc}")

    try:
        bu = parse_note_value(beat_unit)
        if bu <= 0:
            errs.append("Beat unit must be a positive note value")
    except ValueError as exc:
        errs.append(f"Invalid beat unit: {exc}")

    try:
        ts = parse_time_signature(time_sig) if not errs else (4, 4)
        iv = parse_interval(interval, ts)
        if iv <= 0:
            errs.append("Interval must be a positive duration")
    except ValueError as exc:
        errs.append(f"Invalid interval: {exc}")

    return errs


def validate_marker_gen_params(
    bpm: float,
    time_sig: str,
    start_bar: int,
    end_bar: int,
    interval: str,
    fps: float,
    beat_unit: str = "1/4",
) -> list[str]:
    """Return human-readable error strings; empty list = valid."""
    errs: list[str] = []

    if not isinstance(start_bar, int) or start_bar < 1:
        errs.append("Start bar must be an integer ≥ 1")

    if not isinstance(end_bar, int) or end_bar < 1:
        errs.append("End bar must be an integer ≥ 1")
    elif isinstance(start_bar, int) and start_bar >= 1 and end_bar < start_bar:
        errs.append("End bar must be ≥ start bar")

    errs.extend(_validate_common_params(bpm, time_sig, interval, fps, beat_unit))
    return errs


def validate_bar_spec_params(
    bpm: float,
    time_sig: str,
    bar_spec: str,
    interval: str,
    fps: float,
    beat_unit: str = "1/4",
) -> list[str]:
    """Return human-readable error strings; empty list = valid."""
    errs = _validate_common_params(bpm, time_sig, interval, fps, beat_unit)

    try:
        ranges = parse_bar_spec(bar_spec)
        if not ranges:
            errs.append("Bar spec must contain at least one bar or range")
    except ValueError as exc:
        errs.append(f"Invalid bar spec: {exc}")

    return errs
