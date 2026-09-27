"""Tests for config parsing/validation and the QC gates (no network, no sandbox)."""
import json
import os

import pytest

from skyline_pano import config as config_mod
from skyline_pano import qc as qc_mod


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_load_yaml_ok(tmp_path):
    path = _write(tmp_path, "c.yaml", """
source_fps_hint: 60
lines:
  - {name: A, t0: 0, t1: 10, fps: 3}
  - {name: B, t0: 20, t1: 30, fps: 6}
""")
    cfg = config_mod.load(path)
    assert [l.name for l in cfg.lines] == ["A", "B"]
    assert cfg.lines[1].fps == 6.0
    assert cfg.source_fps_hint == 60


def test_fps_floor_enforced(tmp_path):
    path = _write(tmp_path, "c.yaml", "lines:\n  - {name: A, t0: 0, t1: 10, fps: 1}\n")
    cfg = config_mod.load(path)
    assert cfg.lines[0].fps == 3.0  # clamped up to the 3 fps minimum


def test_join_parts_must_exist(tmp_path):
    path = _write(tmp_path, "c.yaml", "lines:\n  - {name: L1, join: [MISSING]}\n")
    with pytest.raises(ValueError):
        config_mod.load(path)


def test_bad_window_rejected(tmp_path):
    path = _write(tmp_path, "c.yaml", "lines:\n  - {name: A, t0: 30, t1: 10, fps: 3}\n")
    with pytest.raises(ValueError):
        config_mod.load(path)


def test_duplicate_names_rejected(tmp_path):
    path = _write(tmp_path, "c.yaml",
                  "lines:\n  - {name: A, t0: 0, t1: 5, fps: 3}\n"
                  "  - {name: A, t0: 6, t1: 9, fps: 3}\n")
    with pytest.raises(ValueError):
        config_mod.load(path)


def test_ordered_longest_first_and_joins_last(tmp_path):
    path = _write(tmp_path, "c.yaml", """
lines:
  - {name: SHORT, t0: 0, t1: 5, fps: 3}
  - {name: LONG, t0: 0, t1: 100, fps: 3}
  - {name: P1, t0: 0, t1: 10, fps: 3}
  - {name: P2, t0: 20, t1: 30, fps: 3}
  - {name: J, join: [P1, P2]}
""")
    cfg = config_mod.load(path)
    assert [l.name for l in cfg.ordered()][-1] == "J"
    # stitchable() is ordered by duration descending (LONG=100, P1=P2=10, SHORT=5)
    assert [l.name for l in cfg.stitchable()] == ["LONG", "P1", "P2", "SHORT"]


def _good_qc():
    return dict(fps=3.0, overlap_min=0.5, h=2160, frame_h=2160, crop_loss_px=0,
                cont_ncc_med=0.85, cont_ncc_min=0.5, cont_r_tx=-0.98, seam_ncc_med=0.6,
                black_frac=0.001, col_step_p99=1.2, sharpness=100.0)


def test_qc_passes_on_good_line():
    r = qc_mod.evaluate(_good_qc(), sharp_src=150.0)
    assert r["passed"] is True
    assert all(c["ok"] for c in r["checks"])


def test_qc_fails_on_low_continuity():
    qc = _good_qc()
    qc["cont_ncc_med"] = 0.2
    r = qc_mod.evaluate(qc, sharp_src=150.0)
    assert r["passed"] is False
    assert any(c["gate"] == "cont_ncc_med>=0.60" and not c["ok"] for c in r["checks"])


def test_qc_fails_on_low_overlap():
    qc = _good_qc()
    qc["overlap_min"] = 0.05
    assert qc_mod.evaluate(qc)["passed"] is False


def test_qc_sharp_ratio_only_when_source_known():
    qc = _good_qc()
    qc["sharpness"] = 10.0
    assert qc_mod.evaluate(qc, sharp_src=-1.0)["passed"] is True
    assert qc_mod.evaluate(qc, sharp_src=150.0)["passed"] is False
