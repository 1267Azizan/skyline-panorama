"""report.py — write the deliverables: config/QC report, JSON manifest, contact sheet."""
import os
import json
import glob

import cv2
import numpy as np


def write_manifest(out_dir, lines, qc_by_line, state, projection="Perspective (plane)",
                   model="pure per-frame translation", min_overlap=0.20, min_fps=3.0,
                   norm_height=2160, source=None):
    """CONFIG_FINAL.json + CONFIG_FINAL.md describing every line."""
    rows = []
    for line in lines:
        png = os.path.join(out_dir, f"{line.name}.png")
        if not os.path.exists(png):
            continue
        im = cv2.imread(png, cv2.IMREAD_UNCHANGED)
        h, w = im.shape[:2]
        del im
        q = qc_by_line.get(line.name, {})
        rows.append(dict(line=line.name, t0=line.t0, t1=line.t1, fps=line.fps,
                         join=line.join or None, w=w, h=h,
                         verdict=state.get(line.name, {}).get("verdict"),
                         frames=q.get("n_frames"), overlap_med=q.get("overlap_med"),
                         overlap_min=q.get("overlap_min"), ncc_med=q.get("cont_ncc_med"),
                         ncc_min=q.get("cont_ncc_min"), r_tx=q.get("cont_r_tx"),
                         seam_med=q.get("seam_ncc_med"), drift_removed=q.get("drift_removed"),
                         black_frac=q.get("black_frac")))

    manifest = dict(projection=projection, model=model, dy_compensation="on (all lines)",
                    min_overlap_required=min_overlap, min_fps=min_fps,
                    elevation_normalised_to=norm_height, source=source, lines=rows)
    with open(os.path.join(out_dir, "CONFIG_FINAL.json"), "w") as f:
        json.dump(manifest, f, indent=1)

    md = ["# Panorama configuration & QC", "",
          f"Projection: **{projection}** · model: {model} · dy compensation: **on** · "
          f"min overlap: **{min_overlap:.0%}** · min fps: **{min_fps:g}**",
          f"Elevation normalised to **{norm_height} px** (one frame tall, level horizon).",
          f"Source: `{source}`" if source else "", "",
          "| Line | t0–t1 (s) | fps | frames | size (px) | ov med | ov min | NCC med | NCC min | r_tx | seam med | drift removed | black |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        span = f"{r['t0']}–{r['t1']}" if r["t0"] is not None else "join " + ",".join(r["join"] or [])
        md.append("| {line} | {span} | {fps} | {frames} | {w}×{h} | {om} | {on} | {nm} | {nn} | {r} | {sm} | {dr} | {bf} |".format(
            line=r["line"], span=span, fps=r["fps"] if r["fps"] else "-", frames=r["frames"] or "-",
            w=r["w"], h=r["h"], om=r["overlap_med"] or "-", on=r["overlap_min"] or "-",
            nm=r["ncc_med"] or "-", nn=r["ncc_min"] or "-", r=r["r_tx"] or "-",
            sm=r["seam_med"] or "-", dr=r["drift_removed"] or "-", bf=r["black_frac"] or "-"))
    with open(os.path.join(out_dir, "CONFIG_FINAL.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    return rows


def write_contact_sheet(out_dir, names, cell_w=1600, filename="ALL_LINES_CONTACT.jpg"):
    """Stack the per-line overviews into one labelled contact sheet."""
    tiles = []
    for name in names:
        p = os.path.join(out_dir, f"{name}_overview.jpg")
        if not os.path.exists(p):
            p = os.path.join(out_dir, f"{name}.png")
        if not os.path.exists(p):
            continue
        im = cv2.imread(p)
        h, w = im.shape[:2]
        im = cv2.resize(im, (cell_w, max(1, int(h * cell_w / w))), interpolation=cv2.INTER_AREA)
        bar = np.full((44, cell_w, 3), 255, np.uint8)
        cv2.putText(bar, f"{name}   {w}x{h}", (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 2)
        tiles.append(np.vstack([bar, im]))
    if not tiles:
        return None
    height = sum(t.shape[0] for t in tiles)
    sheet = np.full((height, cell_w, 3), 240, np.uint8)
    y = 0
    for t in tiles:
        sheet[y:y + t.shape[0]] = t
        y += t.shape[0]
    path = os.path.join(out_dir, filename)
    cv2.imwrite(path, sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return path


def write_readme(out_dir, lines, state, norm_height=2160, projection="Perspective (plane)"):
    ok = [l.name for l in lines if state.get(l.name, {}).get("verdict") == "PASS"]
    body = ["# Output", "",
            f"{len(ok)}/{len(lines)} lines PASS · projection **{projection}** · "
            f"elevation normalised to **{norm_height} px**.", "",
            "Per line: `<LINE>.png` (panorama), `<LINE>_overview.jpg` (preview), "
            "`<LINE>_os.json` (QC). `CONFIG_FINAL.md`/`.json` hold the full configuration. "
            "`ALL_LINES_CONTACT.jpg` is the contact sheet.", ""]
    for line in lines:
        v = state.get(line.name, {})
        body.append(f"- **{line.name}** — {v.get('verdict', 'pending')}"
                    + (f" · {v.get('size')}" if v.get("size") else "")
                    + (f" · {v.get('note')}" if v.get("note") else ""))
    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")
