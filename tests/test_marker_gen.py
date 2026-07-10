"""Tests for marker_gen.py — pure arithmetic, no UI or network."""

import pytest
from fractions import Fraction

from terminal_tc.marker_gen import (
    bar_duration_whole_notes,
    generate_markers,
    generate_markers_from_bar_spec,
    parse_bar_spec,
    parse_interval,
    parse_note_value,
    parse_time_signature,
    seconds_per_beat_unit,
    validate_bar_spec_params,
    validate_marker_gen_params,
)


# ── parse_time_signature ──────────────────────────────────────────────────────


def test_parse_time_sig_4_4():
    assert parse_time_signature("4/4") == (4, 4)


def test_parse_time_sig_6_8():
    assert parse_time_signature("6/8") == (6, 8)


def test_parse_time_sig_3_4():
    assert parse_time_signature("3/4") == (3, 4)


def test_parse_time_sig_invalid_denom():
    with pytest.raises(ValueError, match="power of 2"):
        parse_time_signature("4/6")


def test_parse_time_sig_bad_format():
    with pytest.raises(ValueError):
        parse_time_signature("44")


# ── parse_note_value ──────────────────────────────────────────────────────────


def test_note_whole():
    assert parse_note_value("1") == Fraction(1)


def test_note_half():
    assert parse_note_value("1/2") == Fraction(1, 2)


def test_note_quarter():
    assert parse_note_value("1/4") == Fraction(1, 4)


def test_note_eighth():
    assert parse_note_value("1/8") == Fraction(1, 8)


def test_note_dotted_quarter():
    assert parse_note_value("1/4.") == Fraction(3, 8)


def test_note_dotted_half():
    assert parse_note_value("1/2.") == Fraction(3, 4)


def test_note_triplet_quarter():
    assert parse_note_value("1/4t") == Fraction(1, 6)


def test_note_triplet_eighth():
    assert parse_note_value("1/8t") == Fraction(1, 12)


# ── parse_interval ────────────────────────────────────────────────────────────


def test_interval_note_value():
    assert parse_interval("1/4", (4, 4)) == Fraction(1, 4)


def test_interval_1bar_4_4():
    assert parse_interval("1bar", (4, 4)) == Fraction(1)  # 4/4 = 1 whole note


def test_interval_1bar_6_8():
    assert parse_interval("1bar", (6, 8)) == Fraction(3, 4)  # 6/8 = 6×(1/8)


def test_interval_half_bar():
    assert parse_interval("0.5bar", (4, 4)) == Fraction(1, 2)


def test_interval_2bar():
    assert parse_interval("2bar", (4, 4)) == Fraction(2)


def test_interval_beat_4_4():
    # one denominator-beat in 4/4 = 1/4
    assert parse_interval("beat", (4, 4)) == Fraction(1, 4)


def test_interval_beat_6_8():
    # one denominator-beat in 6/8 = 1/8
    assert parse_interval("beat", (6, 8)) == Fraction(1, 8)


# ── bar_duration_whole_notes ──────────────────────────────────────────────────


def test_bar_dur_4_4():
    assert bar_duration_whole_notes((4, 4)) == Fraction(1)


def test_bar_dur_6_8():
    assert bar_duration_whole_notes((6, 8)) == Fraction(3, 4)


def test_bar_dur_3_4():
    assert bar_duration_whole_notes((3, 4)) == Fraction(3, 4)


# ── seconds_per_beat_unit ─────────────────────────────────────────────────────


def test_spbu_110bpm():
    assert seconds_per_beat_unit(110) == Fraction(6, 11)


def test_spbu_120bpm():
    assert seconds_per_beat_unit(120) == Fraction(1, 2)


def test_spbu_80bpm():
    assert seconds_per_beat_unit(80) == Fraction(3, 4)


# ── generate_markers: 110 BPM, 4/4, 25fps — hand-verified reference table ────
#
# frames_per_whole_note = (1/(1/4)) × (6/11) × 25 = 600/11
# Bar duration = 1 whole note = 600/11 frames ≈ 54.545 frames
#
# Expected values:
#   Bar 3 Beat 1  → 2 bars       = 1200/11 → floor = 109 → 00:00:04:09
#   Bar 3 Beat 4  → 2¾ bars      = 1650/11 = 150  → 00:00:06:00
#   Bar 4 Beat 1  → 3 bars       = 1800/11 → floor = 163 → 00:00:06:13
#   Bar 6 Beat 3  → 5½ bars      = 3300/11 = 300  → 00:00:12:00
#   Bar 8 Beat 4  → 7¾ bars      = 4650/11 → floor = 422 → 00:00:16:22


@pytest.fixture
def markers_110bpm_per_beat():
    return generate_markers(
        bpm=110,
        time_sig="4/4",
        start_bar=1,
        end_bar=8,
        interval="1/4",
        fps=25,
        name_template="Bar {bar} Beat {beat}",
    )


def _tc(markers, name):
    return str(next(m[2] for m in markers if m[1] == name))


def test_bar3_beat1(markers_110bpm_per_beat):
    assert _tc(markers_110bpm_per_beat, "Bar 3 Beat 1") == "00:00:04:09"


def test_bar3_beat4(markers_110bpm_per_beat):
    assert _tc(markers_110bpm_per_beat, "Bar 3 Beat 4") == "00:00:06:00"


def test_bar4_beat1(markers_110bpm_per_beat):
    assert _tc(markers_110bpm_per_beat, "Bar 4 Beat 1") == "00:00:06:13"


def test_bar6_beat3(markers_110bpm_per_beat):
    assert _tc(markers_110bpm_per_beat, "Bar 6 Beat 3") == "00:00:12:00"


def test_bar8_beat4(markers_110bpm_per_beat):
    assert _tc(markers_110bpm_per_beat, "Bar 8 Beat 4") == "00:00:16:22"


def test_marker_count_8_bars_per_beat(markers_110bpm_per_beat):
    # 8 bars × 4 beats = 32 markers
    assert len(markers_110bpm_per_beat) == 32


def test_ids_are_sequential(markers_110bpm_per_beat):
    for i, (mid, _, _) in enumerate(markers_110bpm_per_beat):
        assert mid == f"{i + 1:04d}"


# ── Dotted interval ───────────────────────────────────────────────────────────
#
# 120 BPM, 4/4, 25fps, beat_unit="1/4":
#   frames_per_wn = (1/(1/4)) × (1/2) × 25 = 50
#   interval 1/4. = 3/8 wn → 3/8 × 50 = 18.75 → floor = 18


def test_dotted_interval_first_two():
    markers = generate_markers(
        bpm=120, time_sig="4/4", start_bar=1, end_bar=4,
        interval="1/4.", fps=25,
    )
    assert str(markers[0][2]) == "00:00:00:00"
    assert str(markers[1][2]) == "00:00:00:18"  # floor(18.75) = 18


# ── Triplet interval ──────────────────────────────────────────────────────────
#
# 120 BPM, 4/4, 25fps:
#   frames_per_wn = 50
#   interval 1/8t = 1/12 wn → 50/12 = 4.166… → floor = 4 for n=1


def test_triplet_interval():
    markers = generate_markers(
        bpm=120, time_sig="4/4", start_bar=1, end_bar=2,
        interval="1/8t", fps=25,
    )
    assert str(markers[0][2]) == "00:00:00:00"
    assert str(markers[1][2]) == "00:00:00:04"   # floor(50/12)
    assert str(markers[2][2]) == "00:00:00:08"   # floor(100/12)


# ── Fractional bar interval ───────────────────────────────────────────────────


def test_half_bar_interval():
    # 0.5bar in 4/4 = 1/2 wn, at 120 BPM / 25fps → 25 frames = 1 second
    markers = generate_markers(
        bpm=120, time_sig="4/4", start_bar=1, end_bar=4,
        interval="0.5bar", fps=25,
    )
    assert str(markers[0][2]) == "00:00:00:00"
    assert str(markers[1][2]) == "00:00:01:00"
    assert str(markers[2][2]) == "00:00:02:00"


# ── Compound meter: 6/8 at ♩.=80 ─────────────────────────────────────────────
#
# beat_unit = "1/4." (dotted quarter, as printed on the chart)
# bar_dur = 6/8 = 3/4 wn
# beat_unit_dur = 3/8 wn
# spbu = 60/80 = 3/4 s
# frames_per_wn = (1/(3/8)) × (3/4) × 25 = (8/3) × (3/4) × 25 = 50
# bar in seconds = (3/4 / (3/8)) × (3/4) = 2 × 3/4 = 3/2 = 1.5 s  ✓


def test_compound_meter_bar_seconds():
    spbu = seconds_per_beat_unit(80)
    bar_dur = bar_duration_whole_notes((6, 8))
    bu = parse_note_value("1/4.")
    bar_seconds = (bar_dur / bu) * spbu
    assert bar_seconds == Fraction(3, 2)  # exactly 1.5 s


def test_compound_meter_bar_markers():
    # At 25fps, 1.5s per bar = 37.5 frames → bar 2 starts at frame 37
    markers = generate_markers(
        bpm=80, time_sig="6/8", start_bar=1, end_bar=4,
        interval="1bar", fps=25, beat_unit="1/4.",
    )
    assert str(markers[0][2]) == "00:00:00:00"
    assert str(markers[1][2]) == "00:00:01:12"  # floor(37.5) = 37 → 1s + 12f
    assert str(markers[2][2]) == "00:00:03:00"  # floor(75.0) = 75 → 3s + 0f


# ── Anchor frames (multi-section) ─────────────────────────────────────────────


def test_anchor_offsets_all_markers():
    # Without anchor: bar 1 at frame 0, bar 2 at frame 50 (120 BPM, 25fps, 4/4)
    a = generate_markers(bpm=120, time_sig="4/4", start_bar=1, end_bar=4, interval="1bar", fps=25)
    # With anchor=200: every frame shifted by 200
    b = generate_markers(bpm=120, time_sig="4/4", start_bar=1, end_bar=4, interval="1bar", fps=25, anchor_frames=200)
    for ma, mb in zip(a, b):
        assert mb[2].frame_number == ma[2].frame_number + 200


def test_skip_beats_interval_positions():
    # interval=1/8 in 4/4 → 8 interval positions per bar
    # skip [4, 8] → positions 1,2,3,5,6,7 per bar
    markers = generate_markers(
        bpm=120, time_sig="4/4", start_bar=1, end_bar=2,
        interval="1/8", fps=25, name_template="Bar {bar} Beat {beat}",
        skip_beats=[4, 8],
    )
    names = [m[1] for m in markers]
    assert names == [
        "Bar 1 Beat 1", "Bar 1 Beat 2", "Bar 1 Beat 3",
        "Bar 1 Beat 5", "Bar 1 Beat 6", "Bar 1 Beat 7",
        "Bar 2 Beat 1", "Bar 2 Beat 2", "Bar 2 Beat 3",
        "Bar 2 Beat 5", "Bar 2 Beat 6", "Bar 2 Beat 7",
    ]


def test_beat_num_counts_interval_steps_not_denominator():
    # With interval=1/8 in 4/4: 8 interval positions per bar, numbered 1-8.
    # Old (broken) behaviour would give beat_nums 1,1,2,2,3,3,4,4
    # Correct behaviour gives beat_nums 1,2,3,4,5,6,7,8
    markers = generate_markers(
        bpm=120, time_sig="4/4", start_bar=1, end_bar=2,
        interval="1/8", fps=25, name_template="Bar {bar} Beat {beat}",
    )
    beat_nums = [int(m[1].split("Beat ")[1]) for m in markers[:8]]
    assert beat_nums == [1, 2, 3, 4, 5, 6, 7, 8]


def test_anchor_name_template_uses_bar_from_song():
    # start_bar=3, so bar numbers in names should be 3, 4, 5, 6
    markers = generate_markers(
        bpm=120, time_sig="4/4", start_bar=3, end_bar=6,
        interval="1bar", fps=25, anchor_frames=0,
        name_template="Bar {bar}",
    )
    names = [m[1] for m in markers]
    assert names == ["Bar 3", "Bar 4", "Bar 5", "Bar 6"]


# ── validate_marker_gen_params ────────────────────────────────────────────────


def test_validate_all_valid():
    assert validate_marker_gen_params(120, "4/4", 1, 8, "1bar", 25) == []


def test_validate_bad_bpm():
    errs = validate_marker_gen_params(-5, "4/4", 1, 8, "1bar", 25)
    assert any("BPM" in e for e in errs)


def test_validate_zero_bpm():
    errs = validate_marker_gen_params(0, "4/4", 1, 8, "1bar", 25)
    assert any("BPM" in e for e in errs)


def test_validate_bad_fps():
    errs = validate_marker_gen_params(120, "4/4", 1, 8, "1bar", 23)
    assert any("Frame rate" in e for e in errs)


def test_validate_end_before_start():
    errs = validate_marker_gen_params(120, "4/4", 5, 3, "1bar", 25)
    assert any("End bar" in e for e in errs)


def test_validate_bad_time_sig():
    errs = validate_marker_gen_params(120, "4/6", 1, 8, "1bar", 25)
    assert any("time signature" in e.lower() for e in errs)


def test_validate_bad_interval():
    errs = validate_marker_gen_params(120, "4/4", 1, 8, "notaninterval", 25)
    assert any("interval" in e.lower() for e in errs)


# ── parse_bar_spec ────────────────────────────────────────────────────────────


def test_bar_spec_single_range():
    assert parse_bar_spec("1-8") == [(1, 8)]


def test_bar_spec_single_bar():
    assert parse_bar_spec("5") == [(5, 5)]


def test_bar_spec_mixed():
    assert parse_bar_spec("1-8,12,20-24") == [(1, 8), (12, 12), (20, 24)]


def test_bar_spec_whitespace_tolerant():
    assert parse_bar_spec(" 1 - 8 , 12 ") == [(1, 8), (12, 12)]


def test_bar_spec_preserves_order_no_dedup():
    assert parse_bar_spec("12,1-2") == [(12, 12), (1, 2)]


def test_bar_spec_empty_string():
    with pytest.raises(ValueError):
        parse_bar_spec("")


def test_bar_spec_trailing_comma():
    with pytest.raises(ValueError):
        parse_bar_spec("1-8,")


def test_bar_spec_non_integer():
    with pytest.raises(ValueError):
        parse_bar_spec("abc")


def test_bar_spec_non_integer_in_range():
    with pytest.raises(ValueError):
        parse_bar_spec("1-abc")


def test_bar_spec_end_before_start():
    with pytest.raises(ValueError):
        parse_bar_spec("8-1")


def test_bar_spec_zero_bar():
    with pytest.raises(ValueError):
        parse_bar_spec("0-5")


def test_bar_spec_bad_range_format():
    with pytest.raises(ValueError):
        parse_bar_spec("1-2-3")


# ── generate_markers_from_bar_spec ───────────────────────────────────────────


def test_bar_spec_generation_matches_single_range():
    # bar_spec="1-4" must produce exactly what generate_markers(start_bar=1, end_bar=4) does
    a = generate_markers(
        bpm=120, time_sig="4/4", start_bar=1, end_bar=4, interval="1bar", fps=25,
    )
    b = generate_markers_from_bar_spec(
        bpm=120, time_sig="4/4", bar_spec="1-4", interval="1bar", fps=25,
    )
    assert a == b


def test_bar_spec_generation_multi_range():
    # 120 BPM, 4/4, 25fps, interval=1bar → 50 frames/bar
    markers = generate_markers_from_bar_spec(
        bpm=120, time_sig="4/4", bar_spec="1-2,5", interval="1bar", fps=25,
    )
    names = [m[1] for m in markers]
    frames = [m[2].frame_number for m in markers]
    assert names == ["Bar 1", "Bar 2", "Bar 5"]
    assert frames == [0, 50, 200]


def test_bar_spec_generation_sorts_out_of_order_ranges():
    # bar_spec lists bar 5 before bars 1-2 — output must still be frame-sorted
    markers = generate_markers_from_bar_spec(
        bpm=120, time_sig="4/4", bar_spec="5,1-2", interval="1bar", fps=25,
    )
    assert [m[1] for m in markers] == ["Bar 1", "Bar 2", "Bar 5"]


def test_bar_spec_generation_renumbers_ids_sequentially():
    markers = generate_markers_from_bar_spec(
        bpm=120, time_sig="4/4", bar_spec="5,1-2", interval="1bar", fps=25,
    )
    for i, (mid, _, _) in enumerate(markers):
        assert mid == f"{i + 1:04d}"


def test_bar_spec_generation_overlapping_ranges_not_deduped():
    # Bar 2 appears in both sub-ranges — expect it twice, no deduplication
    markers = generate_markers_from_bar_spec(
        bpm=120, time_sig="4/4", bar_spec="1-2,2-3", interval="1bar", fps=25,
    )
    names = [m[1] for m in markers]
    assert names == ["Bar 1", "Bar 2", "Bar 2", "Bar 3"]


# ── validate_bar_spec_params ──────────────────────────────────────────────────


def test_validate_bar_spec_all_valid():
    assert validate_bar_spec_params(120, "4/4", "1-8,12", "1bar", 25) == []


def test_validate_bar_spec_bad_bpm():
    errs = validate_bar_spec_params(-5, "4/4", "1-8", "1bar", 25)
    assert any("BPM" in e for e in errs)


def test_validate_bar_spec_bad_fps():
    errs = validate_bar_spec_params(120, "4/4", "1-8", "1bar", 23)
    assert any("Frame rate" in e for e in errs)


def test_validate_bar_spec_bad_time_sig():
    errs = validate_bar_spec_params(120, "4/6", "1-8", "1bar", 25)
    assert any("time signature" in e.lower() for e in errs)


def test_validate_bar_spec_bad_interval():
    errs = validate_bar_spec_params(120, "4/4", "1-8", "notaninterval", 25)
    assert any("interval" in e.lower() for e in errs)


def test_validate_bar_spec_empty():
    errs = validate_bar_spec_params(120, "4/4", "", "1bar", 25)
    assert any("bar spec" in e.lower() for e in errs)


def test_validate_bar_spec_non_integer():
    errs = validate_bar_spec_params(120, "4/4", "1-8,abc", "1bar", 25)
    assert any("bar spec" in e.lower() for e in errs)
