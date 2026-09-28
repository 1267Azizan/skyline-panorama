"""cli.py — command line entry point for the skyline panorama pipeline."""
import argparse
import os
import sys

from . import config as config_mod
from .pipeline import Pipeline


def build_parser():
    p = argparse.ArgumentParser(
        prog="skyline-pano",
        description="Automated perspective panorama pipeline for facade sweeps "
                    "(local decimation + sandbox stitching + elevation normalisation).")
    p.add_argument("--src", required=True, help="source video (the long sweep)")
    p.add_argument("--config", required=True, help="line config (.yaml/.yml/.json)")
    p.add_argument("--out", default="output", help="output directory")
    p.add_argument("--stage", default=None, help="segment cache dir (default <out>/_segments)")
    p.add_argument("--par", type=int, default=3, help="max sandboxes / accounts to use")
    p.add_argument("--sandbox-config", default=None,
                   help="provider config JSON (default: ./sandbox.json)")
    p.add_argument("--only", default=None, help="comma-separated subset of line names")
    p.add_argument("--width", type=int, default=3840, help="extraction width (default 3840)")
    p.add_argument("--height", type=int, default=2160, help="normalised output height (default 2160)")
    p.add_argument("--keep", action="store_true", help="do not release the sandboxes")
    p.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    p.add_argument("--version", action="version", version="skyline-panorama 1.0.0")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not os.path.exists(args.src):
        print(f"source not found: {args.src}", file=sys.stderr)
        return 2
    cfg = config_mod.load(args.config)
    if args.only:
        keep = {s.strip() for s in args.only.split(",") if s.strip()}
        # keep requested lines and any join parts they depend on
        needed = set(keep)
        for line in cfg.lines:
            if line.name in keep:
                needed.update(line.join)
        cfg.lines = [l for l in cfg.lines if l.name in needed]
    pipe = Pipeline(args.src, cfg, args.out, stage_dir=args.stage, par=args.par,
                    keep=args.keep, width=args.width, height=args.height,
                    dry_run=args.dry_run, sandbox_config=args.sandbox_config)
    state = pipe.run()
    npass = sum(1 for v in state.values() if v.get("verdict") == "PASS")
    return 0 if npass else 1


if __name__ == "__main__":
    raise SystemExit(main())
