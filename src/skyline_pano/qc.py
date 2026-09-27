"""qc.py — acceptance gates for one stitched line (independent of the stitcher).

Gates encode the project rules:
  1. CONTINUOUS  : cont_ncc_med>=0.60, cont_ncc_min>=0.40, |cont_r_tx|>=0.95, seam_ncc_med>=0.45
  2. GOOD IMAGE  : black_frac<=0.30, col_step_p99<=5.0, sharp_ratio>=0.55
  3. PERSPECTIVE : the caller asserts the plane/translation model
  4. RULES       : fps>=3.0, overlap_min>=0.20, full frame (h>=frame_h, crop_loss==0)
"""
import os
import subprocess

import cv2
import numpy as np

GATES = dict(fps_min=3.0, overlap_min=0.20, cont_ncc_med=0.60, cont_ncc_min=0.40,
             cont_r_abs=0.95, seam_ncc_med=0.45, black_frac_max=0.30, col_step_p99_max=5.0,
             sharp_ratio_min=0.55)


def source_sharpness(src, t0, t1, width=3840):
    """Laplacian variance of one source frame at the WORK width (the upper bound).

    Must be measured at the SAME width as the panorama: Laplacian variance scales with
    resolution, so comparing a 4K panorama against a 1920-px source frame fails a perfect
    stitch (measured ratio 0.30 at mismatched widths vs ~1.0 when matched).
    """
    cmd = ["ffmpeg", "-v", "error", "-ss", str((float(t0) + float(t1)) / 2), "-i", src,
           "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "rawvideo",
           "-pix_fmt", "gray", "pipe:1"]
    out = subprocess.run(cmd, capture_output=True).stdout
    h = int(round(width * 9 / 16 / 2) * 2)
    if len(out) < width * h:
        return -1.0
    g = np.frombuffer(out[:width * h], np.uint8).reshape(h, width).astype(np.float32)
    return float(cv2.Laplacian(g, cv2.CV_32F).var())


def evaluate(qc, sharp_src=-1.0, gates=None):
    """Return {passed, checks[]} for one line's QC dict."""
    g = dict(GATES)
    if gates:
        g.update(gates)

    def chk(name, ok, value, thr):
        return dict(gate=name, ok=bool(ok), value=value, thr=thr)

    checks = [
        chk("fps>=3", qc["fps"] >= g["fps_min"], qc["fps"], f">={g['fps_min']}"),
        chk("overlap_min>=0.20", qc["overlap_min"] >= g["overlap_min"], qc["overlap_min"],
            f">={g['overlap_min']}"),
        chk("full_frame", qc["h"] >= qc["frame_h"] and qc["crop_loss_px"] == 0,
            f"{qc['h']}/{qc['frame_h']} loss {qc['crop_loss_px']}", "h>=frame_h, loss=0"),
        chk("cont_ncc_med>=0.60", qc["cont_ncc_med"] >= g["cont_ncc_med"], qc["cont_ncc_med"],
            f">={g['cont_ncc_med']}"),
        chk("cont_ncc_min>=0.40", qc["cont_ncc_min"] >= g["cont_ncc_min"], qc["cont_ncc_min"],
            f">={g['cont_ncc_min']}"),
    ]
    r = qc.get("cont_r_tx")
    checks.append(chk("|cont_r_tx|>=0.95", r is not None and abs(r) >= g["cont_r_abs"], r,
                      f"abs>={g['cont_r_abs']}"))
    checks.append(chk("seam_ncc_med>=0.45", qc.get("seam_ncc_med", -1) >= g["seam_ncc_med"],
                      qc.get("seam_ncc_med", -1), f">={g['seam_ncc_med']}"))
    checks.append(chk("black_frac<=0.30", qc["black_frac"] <= g["black_frac_max"],
                      qc["black_frac"], f"<={g['black_frac_max']}"))
    checks.append(chk("col_step_p99<=5.0", qc["col_step_p99"] <= g["col_step_p99_max"],
                      qc["col_step_p99"], f"<={g['col_step_p99_max']}"))
    if sharp_src and sharp_src > 0:
        ratio = qc["sharpness"] / sharp_src
        checks.append(chk("sharp_ratio>=0.55", ratio >= g["sharp_ratio_min"],
                          round(ratio, 3), f">={g['sharp_ratio_min']}"))
    return dict(passed=all(c["ok"] for c in checks), checks=checks, sharp_src=sharp_src)


def format_report(name, result):
    lines = [f"=== QC {name}  ->  {'PASS' if result['passed'] else 'FAIL'}"]
    for c in result["checks"]:
        lines.append(f"  [{'ok  ' if c['ok'] else 'FAIL'}] {c['gate']:24s} {c['value']}  ({c['thr']})")
    return "\n".join(lines)
