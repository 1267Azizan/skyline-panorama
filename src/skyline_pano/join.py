"""join.py — concatenate lines that cover the same facade WITHOUT overlapping.

Two sweeps of the same building that do not share any pixels must be *joined*, not
stitched: placing them side by side is exact because both are already elevation-normalised
to the same height. A narrow neutral separator keeps the two sweeps visually distinct.
"""
import os

import cv2
import numpy as np


def concatenate(paths, out, separator=4, sep_value=255, overview_width=6000):
    """Horizontally concatenate the PNGs in ``paths`` into ``out``."""
    imgs = []
    for p in paths:
        if not os.path.exists(p):
            raise FileNotFoundError(p)
        im = cv2.imread(p)
        if im is None:
            raise RuntimeError(f"cannot read {p}")
        imgs.append(im)
    h = min(im.shape[0] for im in imgs)
    imgs = [im[:h] for im in imgs]
    pieces = []
    for i, im in enumerate(imgs):
        if i:
            pieces.append(np.full((h, separator, 3), sep_value, np.uint8))
        pieces.append(im)
    out_img = np.hstack(pieces)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    cv2.imwrite(out, out_img, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    s = min(1.0, overview_width / out_img.shape[1])
    cv2.imwrite(os.path.splitext(out)[0] + "_overview.jpg",
                cv2.resize(out_img, (int(out_img.shape[1] * s), max(1, int(out_img.shape[0] * s))),
                           interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 90])
    return out_img.shape[1], out_img.shape[0]
