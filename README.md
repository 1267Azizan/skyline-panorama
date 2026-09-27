# Skyline Panorama

Automated **perspective** panorama pipeline for hand-held lateral building-facade sweeps.

Give it one long 4K video and a small YAML file describing the lines (start/end second +
frame rate). It then, **unattended**:

1. cuts each line out of the source video and decimates it to its final fps **locally**
   (a 4K @ 3 fps segment is ~1/20 of the source bitrate — this is what keeps uploads small);
2. spreads the lines over as many **Daytona** sandboxes as the accounts allow, uploading
   each segment with a robust chunked transfer;
3. stitches each line with the built-in **perspective (plane) translation** stitcher and
   **normalises the elevation** so the result is exactly one frame tall (2160 px at 4K),
   level, with essentially no black;
4. downloads every deliverable, runs the **QC gates**, writes a config/QC report and a
   contact sheet;
5. **deletes every sandbox** it created.

Everything is resumable: re-running skips lines already marked `PASS`.

## Install

```bash
python -m pip install -r requirements.txt
# the stitcher itself runs on the sandboxes; locally you only need ffmpeg on PATH
```

## Use

```bash
export DAYTONA_API_KEY=dtn_...            # one or more accounts
python -m skyline_pano run \
    --src /path/to/sweep.mp4 \
    --config config/lines.example.yaml \
    --out  ./output \
    --par  3
```

Useful flags: `--only LINE3,LINE7` (subset), `--keep` (do not delete the sandboxes),
`--stage <dir>` (segment cache), `--dry-run` (plan only).

## Line configuration

```yaml
source_fps_hint: 60          # informational
lines:
  - name: LINE2
    t0: 53                   # start second in the source video
    t1: 160                  # end second
    fps: 3                   # frames per second to keep (>= 3)
  - name: LINE1
    join: [LINE1a, LINE1b]   # concatenate two non-overlapping sweeps
  - name: LINE1a
    t0: 0
    t1: 51
    fps: 6
  - name: LINE1b
    t0: 557
    t1: 573
    fps: 6
```

A line with `join:` is built by horizontally concatenating the listed parts **after**
they are stitched (use it when two sweeps cover the same facade but do not overlap).

## Accounts and limits

Tier-1 Daytona gives **10 vCPU / 10 GiB RAM** per account and a **4 vCPU / 8 GiB**
ceiling per sandbox, so the pipeline runs one `daytona-large` sandbox per account and
balances the longest lines first. Add more accounts with `DAYTONA_API_KEY_2`,
`DAYTONA_API_KEY_3`, … (or a `_dt_keys.json`); more accounts = more parallel lines.

## Layout

```
src/skyline_pano/
  cli.py         command line
  config.py      YAML load + validation
  segment.py     local ffmpeg extraction (serialised)
  transfer.py    chunked upload/download with retries
  daytona.py     multi-account sandbox pool
  stitcher.py    the perspective + elevation-normalisation stitcher (runs on the sandbox)
  qc.py          acceptance gates
  join.py        LINE1a+1b concatenation
  report.py      config/QC markdown + JSON + contact sheet
  pipeline.py    the orchestrator
```

## Notes on the method

* **Projection is perspective only.** The camera translates parallel to a flat facade, so
  the correct merge is translation-only placement on a flat canvas — no cylindrical or
  spherical warping, and never a compounding homography chain.
* **Elevation normalisation** shifts every output column so its first filled row lands on
  row 0. That yields one-frame-tall output with a level horizon. The shift is computed from
  the frame placements (`top[x] = min Yoff` over the frames covering `x`) and smoothed once
  over the whole width.
* The stitcher composites in **column slabs** so a 130 000-px-wide line stays inside an
  8 GiB sandbox.

## License

MIT — see `LICENSE`.
