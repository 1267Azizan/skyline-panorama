"""stitcher.py — perspective (plane) panorama with elevation normalisation.

This module is the ENGINE that runs ON THE SANDBOX. It is deliberately standalone: it
imports only cv2/numpy/stdlib so it can be uploaded and executed as a plain script, and it
also exposes ``main()``/``build()`` for local use.

Model
-----
The camera translates parallel to a flat facade, so the correct merge is **translation on
a plane** (perspective, rectilinear): every frame is inserted WHOLE at its measured
offset. No cylindrical/spherical warping, no compounding homography chain.

Elevation normalisation
-----------------------
During a hand-held sweep the camera rises and falls, so a column of the naive canvas holds
a full frame height of content but sits at a different row. Normalising the elevation
means shifting every column so its content starts at row 0: the output is exactly ONE
FRAME TALL (2160 px at 4K), the horizon is level and the black wedges disappear.

The shift is derived from the frame PLACEMENTS (not the pixels):

    top[x] = min{ Yoff[j] : frame j covers column x }

which is the first filled row of column x. It is smoothed ONCE over the whole width — a
per-slab smoothing would leave black slivers at every slab boundary.

Memory
------
Compositing is done in COLUMN SLABS and frames are read from disk on demand, so a
130 000-px-wide line stays inside a 4 vCPU / 8 GiB sandbox.

Usage: python stitcher.py <src> <t0> <t1> <out> [work_w] [fps] [y0f] [y1f] [tmp]
Env: OS_DY=on|off  OS_TMP  OS_DMAX(400)  OS_SLAB(2048)  OS_EXW(3840)  OS_NOFPS(off)
     OS_NORM(on)
"""
import os
import sys
import json
import glob
import time
import shutil
import heapq
import subprocess

import cv2
import numpy as np

MIN_OVERLAP = 0.20
MIN_FPS = 3.0
_SIFT = None


def log(*a):
    print(*a, flush=True)


def extract(src, t0, t1, fps, work_w, tmp, y0f=0.0, y1f=1.0):
    """Cut [t0,t1] out of ``src`` into JPEG frames.

    ``OS_NOFPS=on`` skips the fps filter: the segment was already decimated to the final
    fps by the caller, so re-applying fps here would double-decimate. The frame set then
    matches the pre-decimated segment exactly.
    """
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    ex_w = min(work_w, int(os.environ.get("OS_EXW", "3840")))
    nofps = os.environ.get("OS_NOFPS", "off").lower() == "on"
    vf = f"scale={ex_w}:-2" if nofps else f"fps={fps},scale={ex_w}:-2"
    if y0f > 0.0 or y1f < 1.0:
        vf += f",crop=iw:ih*{y1f - y0f:.4f}:0:ih*{y0f:.4f}"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t0), "-t", str(t1 - t0),
                    "-i", src, "-vf", vf, "-q:v", "3",
                    os.path.join(tmp, "f_%05d.jpg")], check=True)
    return sorted(glob.glob(os.path.join(tmp, "f_*.jpg")))


def pair_motion(a, b, dmax):
    """Relative shift (dx,dy) from frame a to frame b, in pixels.

    SIFT + affine RANSAC first (robust on textured facades), phase correlation as the
    fallback for low-texture frames.
    """
    global _SIFT
    if _SIFT is None:
        _SIFT = cv2.SIFT_create(nfeatures=6000)
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
    h, w = ga.shape
    ka, da = _SIFT.detectAndCompute(ga, None)
    kb, db = _SIFT.detectAndCompute(gb, None)
    if da is not None and db is not None and len(ka) >= 12 and len(kb) >= 12:
        kn = cv2.BFMatcher().knnMatch(da, db, k=2)
        good = [m for m, n in kn if m.distance < 0.78 * n.distance]
        if len(good) >= 12:
            src = np.float32([ka[m.queryIdx].pt for m in good])
            dst = np.float32([kb[m.trainIdx].pt for m in good])
            m, inl = cv2.estimateAffine2D(src, dst, method=cv2.RANSAC,
                                          ransacReprojThreshold=3.0, maxIters=4000,
                                          confidence=0.995, refineIters=20)
            if m is not None and inl is not None and int(inl.sum()) >= 10:
                c = np.array([w / 2.0, h / 2.0, 1.0])
                d = m @ c - c[:2]
                dx, dy = float(d[0]), float(d[1])
                if abs(dx) <= 1.5 * w and abs(dy) <= dmax:
                    return dx, dy, int(inl.sum()), "sift"
    y0, y1 = int(h * 0.15), int(h * 0.70)
    win = cv2.createHanningWindow((w, y1 - y0), cv2.CV_32F)
    (dx, dy), _ = cv2.phaseCorrelate(ga[y0:y1].astype(np.float32) * win,
                                     gb[y0:y1].astype(np.float32) * win)
    if abs(dy) > dmax:
        dy = 0.0
    return float(dx), float(dy), 0, "pc"


def top_profile(placements, frame_w, yoff, canvas_w):
    """First filled row of every column: min Yoff over the frames covering that column.

    Computed from the placements only (no image) with a min-heap sweep, so it is cheap
    even for a 130 000-px canvas. Columns outside every frame span are filled from their
    nearest valid neighbour so the later smoothing cannot smear inf/NaN inward.
    """
    top = np.full(canvas_w, np.inf, np.float32)
    starts, ends = {}, {}
    for j in range(len(placements)):
        lo = int(np.round(placements[j]))
        starts.setdefault(lo, []).append(j)
        ends.setdefault(lo + frame_w, []).append(j)
    heap, active = [], set()
    for x in range(canvas_w):
        for j in starts.get(x, ()):
            heapq.heappush(heap, (yoff[j], j))
            active.add(j)
        for j in ends.get(x, ()):
            active.discard(j)
        while heap and heap[0][1] not in active:
            heapq.heappop(heap)
        if heap:
            top[x] = heap[0][0]
    bad = ~np.isfinite(top)
    if bad.any() and (~bad).any():
        good = np.where(~bad)[0]
        top[bad] = np.interp(np.where(bad)[0], good, top[good])
    return top


def build(src, t0, t1, out, work_w=3840, fps=MIN_FPS, y0f=0.0, y1f=1.0,
          tmp=None, use_dy=True, dmax=400.0, slab=2048, norm=True):
    """Stitch ``src[t0:t1]`` into ``out`` and return the QC dict."""
    tmp = tmp or os.environ.get("OS_TMP", "_os_tmp")
    t00 = time.time()

    paths, dxs, w = [], [], work_w
    for attempt in range(5):
        paths = extract(src, t0, t1, round(fps, 3), work_w, tmp, y0f, y1f)
        if len(paths) < 2:
            raise SystemExit("too few frames")
        prev, dxs = None, []
        for p in paths:
            g = cv2.imread(p)
            if g is None:
                continue
            w = g.shape[1]
            if prev is not None:
                dx, _, _, _ = pair_motion(prev, g, dmax)
                dxs.append(abs(dx))
            prev = g
        ovs = [1.0 - d / float(w) for d in dxs]
        log(f"[stitch] attempt {attempt + 1}: {len(paths)} frames @ {fps:.2f}fps {w}px | "
            f"ov med {np.median(ovs) * 100:.1f}% min {min(ovs) * 100:.1f}%")
        if min(ovs) >= MIN_OVERLAP:
            break
        fps = max(fps * 1.6, fps * (np.median(dxs) / (0.8 * w)) * 1.15)
        shutil.rmtree(tmp, ignore_errors=True)
        log(f"[stitch] overlap<20% -> fps {fps:.2f}")

    ov_med = float(np.median(ovs)) if ovs else -1
    ov_min = float(min(ovs)) if ovs else -1

    n = len(paths)
    f0 = cv2.imread(paths[0])
    h, w = f0.shape[:2]
    del f0
    dx_list, dy_list = [0.0], [0.0]
    prev = cv2.imread(paths[0])
    for j in range(1, n):
        cur = cv2.imread(paths[j])
        dx, dy, _, _ = pair_motion(prev, cur, dmax)
        dx_list.append(dx)
        dy_list.append(dy if use_dy else 0.0)
        prev = cur
    del prev
    dx_arr = np.array(dx_list)
    dy_arr = np.array(dy_list)
    log(f"[stitch] motion dx sum {dx_arr.sum():+.0f}px | dy sum {dy_arr.sum():+.0f}px")

    p = -np.cumsum(dx_arr)
    p = p - p.min()
    y = -np.cumsum(dy_arr)
    yoff = np.round(y - y.min()).astype(int)
    canvas_w = int(round(p.max())) + w + 2
    canvas_h = h + int(yoff.max() - yoff.min())
    log(f"[stitch] canvas {canvas_w}x{canvas_h} (frame H={h}, y-spread {int(yoff.max() - yoff.min())})")

    means = []
    for path in paths:
        g = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        means.append(float(g.mean()) if g is not None else 0.0)
    gains = np.clip(float(np.median(means)) / np.maximum(np.array(means), 1e-6),
                    0.55, 1.60).astype(np.float32)

    centers = (p + w / 2.0).astype(np.float32)
    blend = max(2, int(round(w * 0.12)) // 2)
    lo_all = np.round(p).astype(int)
    hi_all = lo_all + w

    if norm:
        top = top_profile(p, w, yoff, canvas_w)
        k = max(51, (canvas_w // 200) | 1)
        kern = np.ones(k, np.float32) / k
        top_s = np.convolve(np.pad(top, (k // 2, k // 2), mode="edge"), kern, mode="valid")[:canvas_w]
        src_top = np.clip(np.rint(top_s).astype(np.int32), 0, max(0, canvas_h - h))
        out_h = h
    else:
        src_top = np.zeros(canvas_w, np.int32)
        out_h = canvas_h

    img = np.zeros((out_h, canvas_w, 3), np.uint8)
    covcols = np.zeros(out_h, np.int64)
    nslab = (canvas_w + slab - 1) // slab
    rows_out = np.arange(out_h, dtype=np.int32)
    for si, x0 in enumerate(range(0, canvas_w, slab)):
        x1 = min(canvas_w, x0 + slab)
        cw = x1 - x0
        acc = np.zeros((canvas_h, cw, 3), np.float32)
        wsum = np.zeros((canvas_h, cw), np.float32)
        xs = np.arange(x0, x1, dtype=np.float32)
        d = np.abs(xs[None, :] - centers[:, None]).astype(np.float32)
        order = np.argsort(d, axis=0)
        j1, j2 = order[0], order[1]
        ii = np.arange(cw)
        d1, d2 = d[j1, ii], d[j2, ii]
        t2 = np.clip(0.5 + (d2 - d1) / (2 * blend), 0, 1).astype(np.float32)
        for j in np.where((hi_all > x0) & (lo_all < x1))[0]:
            frame = cv2.imread(paths[int(j)])
            if frame is None:
                continue
            lo = int(lo_all[j])
            a, b = max(x0, lo), min(x1, lo + w)
            if b <= a:
                del frame
                continue
            cols = np.clip(np.arange(a, b) - lo, 0, w - 1)
            wt = np.where(j1 == j, t2, np.where(j2 == j, 1.0 - t2, 0.0)).astype(np.float32)
            wsel = wt[a - x0:b - x0]
            if wsel.max() <= 1e-4:
                del frame
                continue
            s0 = int(yoff[j])
            s1 = min(s0 + h, canvas_h)
            if s1 <= s0:
                del frame
                continue
            acc[s0:s1, a - x0:b - x0] += (frame[:s1 - s0][:, cols].astype(np.float32) * gains[j]) * wsel[None, :, None]
            wsum[s0:s1, a - x0:b - x0] += wsel[None, :]
            del frame
        cov = acc / np.maximum(wsum, 1e-6)[:, :, None]
        filled = wsum > 1e-6
        if norm:
            idx = src_top[x0:x1][None, :] + rows_out[:, None]
            np.clip(idx, 0, canvas_h - 1, out=idx)
            slab_img = np.take_along_axis(cov, idx[:, :, None], axis=0)
            covcols += np.take_along_axis(filled, idx, axis=0).sum(1)
        else:
            slab_img = cov
            covcols += filled.sum(1)
        img[:, x0:x1] = np.clip(slab_img, 0, 255).astype(np.uint8)
        del acc, wsum, cov, filled, slab_img
        if si % 5 == 0 or si == nslab - 1:
            log(f"[stitch] slab {si + 1}/{nslab} x={x0}-{x1} {time.time() - t00:.0f}s")

    covered = np.where(covcols > 0)[0]
    ct, cb = int(covered[0]), int(covered[-1])
    img = img[ct:cb + 1]
    crop_loss = out_h - (cb - ct + 1)

    # black fraction + inpaint of tiny holes, chunked
    black_px = tot_px = 0
    for x0 in range(0, img.shape[1], 8192):
        x1 = min(img.shape[1], x0 + 8192)
        sub = img[:, x0:x1]
        m = (sub[:, :, 0] == 0) & (sub[:, :, 1] == 0) & (sub[:, :, 2] == 0)
        black_px += int(m.sum())
        tot_px += int(m.size)
        if m.any():
            nlab, lab = cv2.connectedComponents(m.astype(np.uint8), connectivity=4)
            if nlab > 1:
                cnt = np.bincount(lab.ravel())
                small = np.where(cnt < 4000)[0]
                small = small[small != 0]
                if len(small):
                    mm = np.isin(lab, small).astype(np.uint8) * 255
                    img[:, x0:x1] = cv2.inpaint(sub, mm, 3, cv2.INPAINT_TELEA)
        del sub, m
    black_frac = black_px / float(max(1, tot_px))

    # per-column exposure gain on a downscaled copy, then apply chunked
    small = cv2.resize(img, (max(1, img.shape[1] // 16), max(1, img.shape[0] // 16)),
                       interpolation=cv2.INTER_AREA)
    gs = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
    prof_s = cv2.GaussianBlur(gs.mean(0).reshape(1, -1), (0, 0), sigmaX=12).ravel()
    gcol_s = np.clip(float(np.median(prof_s)) / np.maximum(prof_s, 1e-3), 0.88, 1.15)
    gcol = cv2.resize(gcol_s.reshape(1, -1).astype(np.float32),
                      (img.shape[1], 1), interpolation=cv2.INTER_LINEAR).ravel()
    for x0 in range(0, img.shape[1], 8192):
        x1 = min(img.shape[1], x0 + 8192)
        sub = img[:, x0:x1].astype(np.float32)
        sub *= gcol[x0:x1][None, :, None]
        np.clip(sub, 0, 255, out=sub)
        sub = sub.astype(np.uint8)
        bl = cv2.GaussianBlur(sub, (0, 0), 1.0)
        img[:, x0:x1] = cv2.addWeighted(sub, 1.12, bl, -0.12, 0)
        del sub, bl

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    cv2.imwrite(out, img, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    s = min(1.0, 6000.0 / img.shape[1])
    cv2.imwrite(os.path.splitext(out)[0] + "_overview.jpg",
                cv2.resize(img, (int(img.shape[1] * s), max(1, int(img.shape[0] * s))),
                           interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 90])
    log(f"[stitch] wrote {out} {img.shape[1]}x{img.shape[0]}")

    # object-continuity: match each frame's centre band into the panorama (windowed)
    pg = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hp, wp = pg.shape
    nccs, xs = [], []
    pred_x = np.round(p).astype(int)
    idx_qc = np.arange(n)
    if n > 60:
        idx_qc = np.unique(np.linspace(0, n - 1, 60).round().astype(int))
    for j in idx_qc:
        g = cv2.imread(paths[int(j)], cv2.IMREAD_GRAYSCALE)
        if g is None:
            continue
        gh, gw = g.shape
        th = min(int(gh * 0.5), hp - 1)
        t = g[int(gh * 0.25):int(gh * 0.25) + th, int(gw * 0.42):int(gw * 0.58)]
        if t.shape[0] < 32 or t.shape[1] < 16 or t.shape[0] >= hp or t.shape[1] >= wp:
            continue
        cx = int(pred_x[j] + gw * 0.42)
        a = max(0, cx - 1500)
        b = min(wp, cx + 1500 + t.shape[1])
        if b - a < t.shape[1] + 8:
            continue
        res = cv2.matchTemplate(pg[:, a:b], t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, ml = cv2.minMaxLoc(res)
        nccs.append(float(mx))
        xs.append(int(a + ml[0]))
    r = float(np.corrcoef(np.arange(len(xs)), xs)[0, 1]) if len(xs) >= 3 else float("nan")

    seam = []
    prev = cv2.imread(paths[0], cv2.IMREAD_GRAYSCALE)
    for j in range(1, n):
        cur = cv2.imread(paths[j], cv2.IMREAD_GRAYSCALE)
        w_ov = int(round(w - dx_arr[j]))
        if w_ov >= 64:
            dy = int(yoff[j - 1] - yoff[j])
            y0, y1 = max(0, dy), min(h, h + dy)
            ya0 = y0 + int((y1 - y0) * 0.20)
            ya1 = y0 + int((y1 - y0) * 0.70)
            if ya1 - ya0 >= 64:
                aa = prev[ya0:ya1, 0:w_ov]
                bb = cur[ya0 + dy:ya1 + dy, w - w_ov:w]
                if aa.shape == bb.shape and aa.size > 100:
                    a2 = aa.astype(np.float32) - aa.mean()
                    b2 = bb.astype(np.float32) - bb.mean()
                    dd = np.sqrt((a2 * a2).sum() * (b2 * b2).sum())
                    seam.append(float((a2 * b2).sum() / dd) if dd > 0 else -1.0)
        prev = cur

    g2 = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    cs = np.abs(np.diff(g2.mean(0)))
    qc = dict(w=int(img.shape[1]), h=int(img.shape[0]), frame_h=int(h), n_frames=n,
              fps=round(fps, 3), overlap_med=round(ov_med, 4), overlap_min=round(ov_min, 4),
              use_dy=bool(use_dy), dy_sum=round(float(dy_arr.sum()), 1),
              dx_sum=round(float(dx_arr.sum()), 1),
              y_spread=int(yoff.max() - yoff.min()),
              cont_ncc_med=round(float(np.median(nccs)), 3) if nccs else -1,
              cont_ncc_min=round(float(np.min(nccs)), 3) if nccs else -1,
              cont_r_tx=round(r, 4) if r == r else None,
              seam_ncc_med=round(float(np.median(seam)), 3) if seam else -1,
              seam_ncc_min=round(float(np.min(seam)), 3) if seam else -1,
              black_frac=round(black_frac, 4), crop_loss_px=int(crop_loss),
              sharpness=round(float(cv2.Laplacian(g2, cv2.CV_32F).var()), 1),
              col_step_p99=round(float(np.percentile(cs, 99)), 3),
              norm=bool(norm), h_before=int(canvas_h), h_after=int(out_h),
              drift_removed=int(canvas_h - out_h) if norm else 0,
              secs=round(time.time() - t00, 0))
    json.dump(qc, open(os.path.splitext(out)[0] + "_os.json", "w"), indent=1)
    log("[stitch] QC " + json.dumps(qc))
    return qc


def main():
    src, t0, t1, out = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
    work_w = int(sys.argv[5]) if len(sys.argv) > 5 else 3840
    fps = max(float(sys.argv[6]) if len(sys.argv) > 6 else MIN_FPS, MIN_FPS)
    y0f = float(sys.argv[7]) if len(sys.argv) > 7 else 0.0
    y1f = float(sys.argv[8]) if len(sys.argv) > 8 else 1.0
    tmp = sys.argv[9] if len(sys.argv) > 9 else os.environ.get("OS_TMP", "_os_tmp")
    build(src, t0, t1, out, work_w=work_w, fps=fps, y0f=y0f, y1f=y1f, tmp=tmp,
          use_dy=os.environ.get("OS_DY", "on").lower() != "off",
          dmax=float(os.environ.get("OS_DMAX", "400")),
          slab=int(os.environ.get("OS_SLAB", "2048")),
          norm=os.environ.get("OS_NORM", "on").lower() != "off")


if __name__ == "__main__":
    main()
