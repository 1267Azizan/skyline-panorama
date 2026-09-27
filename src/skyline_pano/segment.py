"""segment.py — cut each line out of the source video, locally, at its FINAL fps.

Why this exists: the source is a 4K60 file. Uploading it whole (or even per line at 60 fps)
is the real bottleneck, and the uplink is only ~1.3 MB/s. Decimating to the final fps
*before* upload shrinks a segment to roughly 1/20 of the source bitrate, so uploads are
small and the sandbox does no decimation (it stitches the exact frame set we chose).

Extraction is SERIALISED by a lock: three concurrent 4K x264 encodes exhaust local RAM
(x264 reports "malloc failed"). One at a time is a few seconds per segment.
"""
import os
import subprocess
import threading

_LOCK = threading.Lock()


class Segmenter:
    def __init__(self, src, stage, width=3840, height=2160, crf=20, threads=2):
        self.src = src
        self.stage = stage
        self.width = width
        self.height = height
        self.crf = crf
        self.threads = threads
        os.makedirs(stage, exist_ok=True)

    def path_for(self, line):
        return os.path.join(self.stage, f"{line.name}_{line.fps:g}fps.mp4")

    def make(self, line, force=False):
        """Return the path of the decimated segment, creating it if needed."""
        seg = self.path_for(line)
        if not force and os.path.exists(seg) and os.path.getsize(seg) > 100_000:
            return seg
        with _LOCK:
            if not force and os.path.exists(seg) and os.path.getsize(seg) > 100_000:
                return seg
            tmp = seg + ".part.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-v", "error",
                "-ss", str(line.t0), "-t", str(line.duration), "-i", self.src,
                "-vf", f"fps={line.fps:g},scale={self.width}:{self.height}:flags=lanczos",
                "-c:v", "libx264", "-crf", str(self.crf), "-preset", "veryfast",
                "-threads", str(self.threads), "-pix_fmt", "yuv420p", "-an", tmp,
            ], check=True)
            os.replace(tmp, seg)
        return seg

    def duration_ok(self, line, seg):
        """True when the produced segment really covers the requested window."""
        want = line.duration
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", seg],
                capture_output=True, text=True, check=True).stdout.strip()
            got = float(out)
        except Exception:
            return False
        return abs(got - want) <= max(0.5, want * 0.05)
