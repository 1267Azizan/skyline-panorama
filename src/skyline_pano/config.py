"""config.py — load and validate the line configuration (YAML or JSON)."""
import os
import json

try:
    import yaml
except ImportError:  # YAML is optional; JSON configs still work
    yaml = None

MIN_FPS = 3.0


class Line:
    """One output panorama: either a single source window or a concatenation of parts."""

    def __init__(self, name, t0=None, t1=None, fps=None, join=None):
        self.name = name
        self.t0 = t0
        self.t1 = t1
        self.fps = fps
        self.join = join or []
        if self.join:
            if not self.join:
                raise ValueError(f"{name}: 'join' must list at least one part")
        else:
            if t0 is None or t1 is None:
                raise ValueError(f"{name}: needs t0 and t1 (or a 'join' list)")
            if t1 <= t0:
                raise ValueError(f"{name}: t1 must be greater than t0")
            self.fps = max(float(fps if fps is not None else MIN_FPS), MIN_FPS)

    @property
    def duration(self):
        return 0.0 if self.join else float(self.t1) - float(self.t0)

    def as_dict(self):
        if self.join:
            return dict(name=self.name, join=list(self.join))
        return dict(name=self.name, t0=self.t0, t1=self.t1, fps=self.fps)


class Config:
    def __init__(self, lines, source_fps_hint=None):
        self.lines = lines
        self.source_fps_hint = source_fps_hint
        by_name = {l.name: l for l in lines}
        for l in lines:
            for part in l.join:
                if part not in by_name:
                    raise ValueError(f"{l.name}: join part '{part}' is not defined")
                if by_name[part].join:
                    raise ValueError(f"{l.name}: join part '{part}' is itself a join")

    def ordered(self):
        """Longest single lines first (best load balance), joins last."""
        return sorted(self.lines, key=lambda l: (bool(l.join), -l.duration))

    def stitchable(self):
        """Only the lines that are stitched, longest first."""
        return [l for l in self.ordered() if not l.join]


def _parse(data):
    raw = data.get("lines")
    if not isinstance(raw, list) or not raw:
        raise ValueError("config must contain a non-empty 'lines' list")
    lines = []
    seen = set()
    for item in raw:
        if "name" not in item:
            raise ValueError(f"line entry missing 'name': {item}")
        if item["name"] in seen:
            raise ValueError(f"duplicate line name: {item['name']}")
        seen.add(item["name"])
        lines.append(Line(item["name"], item.get("t0"), item.get("t1"),
                          item.get("fps"), item.get("join")))
    return Config(lines, data.get("source_fps_hint"))


def load(path):
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.lower().endswith((".yaml", ".yml")):
        if yaml is None:
            raise RuntimeError("PyYAML is required for .yaml configs: pip install pyyaml")
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return _parse(data)
