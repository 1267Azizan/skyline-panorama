# Skyline Panorama

Automated **perspective** panorama pipeline for hand-held lateral building-facade sweeps.

Give it one long 4K video and a small YAML file describing the lines (start/end second +
frame rate). It then, **unattended**:

1. cuts each line out of the source video and decimates it to its final fps **before**
   upload (a 4K @ 3 fps segment is ~1/20 of the source bitrate — this keeps the transfer
   small);
2. spreads the lines over as many **hosted sandboxes** as your accounts allow, uploading
   each segment with a robust chunked transfer;
3. stitches each line with the built-in **perspective (plane) translation** stitcher and
   **normalises the elevation** so the result is exactly one frame tall (2160 px at 4K),
   level, with essentially no black;
4. downloads every deliverable, runs the **QC gates**, writes a config/QC report and a
   contact sheet;
5. **releases every sandbox** it created.

Everything is resumable: re-running skips lines already marked `PASS`.

## Install

```bash
python -m pip install -r requirements.txt
# the stitcher itself runs on the sandboxes; you only need ffmpeg on PATH
```

## Provider configuration

No provider name is hard-coded. Copy the example and fill it in (the real file is
git-ignored):

```bash
cp sandbox.example.json sandbox.json
```

```json
{
  "sdk": "your_provider_sdk",
  "client_class": "Client",
  "params_class": "CreateSandboxParams",
  "key_env": "SKYLINE_SANDBOX_KEY",
  "snapshot": "your-snapshot-name",
  "remote_home": "/home/user",
  "api_base": "https://api.your-provider.example",
  "keys": []
}
```

Any SDK that exposes `Client()`, `client.create(params)`, `client.get(id)`,
`client.delete(sb)`, `sb.process.exec(cmd)` and `sb.fs.upload_file` / `download_file`
works. Accounts are isolated and each carries its own quota, so **more accounts = more
parallel lines**: put one key per account in `keys`, or set `SKYLINE_SANDBOX_KEY`,
`SKYLINE_SANDBOX_KEY_2`, …

## Use

```bash
python -m skyline_pano \
    --src /path/to/sweep.mp4 \
    --config config/lines.example.yaml \
    --out  ./output \
    --par  3
```

Useful flags: `--only LINE3,LINE7` (subset), `--keep` (leave the sandboxes running),
`--stage <dir>` (segment cache), `--sandbox-config <file>`, `--dry-run` (plan only).

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

## Layout

```
src/skyline_pano/
  cli.py         command line
  config.py      YAML load + validation
  segment.py     ffmpeg extraction (serialised)
  transfer.py    chunked upload/download with retries
  remote.py      provider-neutral sandbox pool (multi-account)
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
* The stitcher composites in **column slabs** so a very wide line stays inside a modest
  sandbox.

## License

MIT — see `LICENSE`.
