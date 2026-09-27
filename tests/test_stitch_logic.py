"""Tests for the stitcher's pure elevation logic and the join helper (no ffmpeg, no net)."""
import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from skyline_pano.stitcher import top_profile  # noqa: E402
from skyline_pano import join as join_mod  # noqa: E402


def test_top_profile_single_frame_is_its_offset():
    # one frame of width 10 at x=0 with vertical offset 7
    top = top_profile(np.array([0.0]), frame_w=10, yoff=np.array([7]), canvas_w=10)
    assert np.allclose(top, 7.0)


def test_top_profile_takes_the_minimum_over_overlapping_frames():
    # frame A at x=0..10 (yoff 20), frame B at x=5..15 (yoff 5)
    placements = np.array([0.0, 5.0])
    yoff = np.array([20, 5])
    top = top_profile(placements, frame_w=10, yoff=yoff, canvas_w=15)
    # columns 0..4 are covered only by A -> 20; columns 5..9 by both -> min = 5
    assert np.allclose(top[:5], 20.0)
    assert np.allclose(top[5:10], 5.0)


def test_top_profile_fills_uncovered_columns():
    # frame covers x=0..10 only; columns 10..14 are uncovered and must not be inf/NaN
    top = top_profile(np.array([0.0]), frame_w=10, yoff=np.array([3]), canvas_w=15)
    assert np.isfinite(top).all()
    assert np.allclose(top, 3.0)


def test_join_concatenates_side_by_side(tmp_path):
    a = np.full((20, 30, 3), 10, np.uint8)
    b = np.full((20, 40, 3), 200, np.uint8)
    pa, pb = tmp_path / "a.png", tmp_path / "b.png"
    cv2.imwrite(str(pa), a)
    cv2.imwrite(str(pb), b)
    out = tmp_path / "joined.png"
    w, h = join_mod.concatenate([str(pa), str(pb)], str(out), separator=4)
    assert (w, h) == (30 + 4 + 40, 20)
    got = cv2.imread(str(out))
    assert got.shape[:2] == (20, 74)
    assert (got[:, :30] == 10).all() and (got[:, -40:] == 200).all()


def test_join_trims_to_common_height(tmp_path):
    a = np.full((20, 10, 3), 1, np.uint8)
    b = np.full((18, 10, 3), 2, np.uint8)  # shorter
    pa, pb = tmp_path / "a.png", tmp_path / "b.png"
    cv2.imwrite(str(pa), a)
    cv2.imwrite(str(pb), b)
    out = tmp_path / "joined.png"
    _, h = join_mod.concatenate([str(pa), str(pb)], str(out), separator=0)
    assert h == 18
