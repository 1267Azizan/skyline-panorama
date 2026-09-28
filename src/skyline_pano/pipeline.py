"""pipeline.py — the orchestrator: config in, deliverables out, unattended.

Flow
----
1. load config, decide which lines still need work (resume)
2. bring up the sandbox pool (one sandbox per configured account)
3. push the stitcher + deps to every sandbox
4. per sandbox, in parallel: cut its segments and upload them
5. launch each sandbox's lines sequentially (a 4K canvas is memory-heavy) and poll
6. download every deliverable, run the QC gates, mark PASS/PARTIAL
7. join concatenated lines (e.g. LINE1a+LINE1b -> LINE1)
8. write the config/QC report, contact sheet and README
9. release every sandbox it created
"""
import os
import json
import time
import threading

from . import config as config_mod
from . import transfer
from . import qc as qc_mod
from . import join as join_mod
from . import report as report_mod
from .segment import Segmenter
from .remote import Pool

STATE_FILE = "state.json"
STITCHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stitcher.py")


def _log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


class Pipeline:
    def __init__(self, src, cfg, out_dir, stage_dir=None, par=3, keep=False,
                 width=3840, height=2160, dry_run=False, sandbox_config=None):
        self.src = src
        self.cfg = cfg
        self.out = out_dir
        self.stage = stage_dir or os.path.join(out_dir, "_segments")
        self.par = par
        self.keep = keep
        self.width = width
        self.height = height
        self.dry_run = dry_run
        self.sandbox_config = sandbox_config
        self.seg = Segmenter(src, self.stage, width=width, height=height)
        self.state_path = os.path.join(out_dir, STATE_FILE)
        self.state = {}
        self.qc_by_line = {}
        self._home = "/home/user"

    # ---------- state ----------
    def load_state(self):
        os.makedirs(self.out, exist_ok=True)
        if os.path.exists(self.state_path):
            try:
                self.state = json.load(open(self.state_path))
            except ValueError:
                self.state = {}
        return self.state

    def save_state(self):
        json.dump(self.state, open(self.state_path, "w"), indent=1)

    def _done(self, name):
        return self.state.get(name, {}).get("verdict") == "PASS"

    # ---------- planning ----------
    def plan(self):
        todo = [l for l in self.cfg.stitchable() if not self._done(l.name)]
        joins = [l for l in self.cfg.lines if l.join
                 and all(self._done(p) for p in l.join) and not self._done(l.name)]
        return todo, joins

    # ---------- run ----------
    def run(self):
        self.load_state()
        todo, joins = self.plan()
        _log(f"config: {len(self.cfg.lines)} lines ({len(self.cfg.stitchable())} stitchable, "
             f"{len(self.cfg.lines) - len(self.cfg.stitchable())} join) · to do: "
             f"{[l.name for l in todo]}")
        if self.dry_run:
            for line in self.cfg.ordered():
                kind = "join " + ",".join(line.join) if line.join else f"t{line.t0}-{line.t1} @ {line.fps}fps"
                _log(f"  {line.name:8s} {kind}")
            return self.state

        if todo:
            pool = Pool(config_path=self.sandbox_config, max_accounts=self.par, log=_log)
            units = pool.up()
            self._home = getattr(units[0], "home", self._home) if units else self._home
            try:
                self._prepare(units)
                self._stage_and_run(units, todo)
            finally:
                if not self.keep:
                    pool.down()
                else:
                    _log("--keep: sandboxes left running")

        self._do_joins(joins)
        self._finalize()
        return self.state

    # ---------- setup ----------
    def _prepare(self, units):
        def setup(u):
            u.install_deps()
            ok, _ = u.upload(STITCHER, "stitcher.py", part_mb=16, workers=2)
            if not ok:
                _log(f"  [{u.label}] stitcher upload failed")
        ths = [threading.Thread(target=setup, args=(u,), daemon=True) for u in units]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        _log("sandboxes ready (deps + stitcher)")

    # ---------- stage + run ----------
    def _stage_and_run(self, units, todo):
        for u in units:
            u.lines = []
        for i, line in enumerate(sorted(todo, key=lambda l: -l.duration)):
            units[i % len(units)].lines.append(line)
        for u in units:
            _log(f"  [{u.label}] {[l.name for l in u.lines]}")

        def work(u):
            ready = []
            for line in u.lines:
                try:
                    seg = self.seg.make(line)
                except Exception as e:  # noqa: BLE001
                    _log(f"  [{u.label}] {line.name} extract failed: {type(e).__name__}")
                    continue
                if not self.seg.duration_ok(line, seg):
                    _log(f"  [{u.label}] {line.name} segment duration mismatch, re-extracting")
                    seg = self.seg.make(line, force=True)
                want = str(os.path.getsize(seg))
                ok = False
                for att in range(4):
                    if transfer.remote_size(u.sb, f"{line.name}_seg.mp4", home=u.home) == want:
                        ok = True
                        break
                    ok, mbs = u.upload(seg, f"{line.name}_seg.mp4", part_mb=8, workers=8)
                    _log(f"    [{u.label}] upload {line.name} "
                         f"({os.path.getsize(seg) / 1e6:.0f}MB) try{att + 1} ok={ok} {mbs:.2f} MB/s")
                    if ok:
                        break
                if ok:
                    ready.append(line)
            u.ready = ready

        ths = [threading.Thread(target=work, args=(u,), daemon=True) for u in units]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        _log("segments staged")

        for u in units:
            self._launch(u)

        self._poll(units)

    def _launch(self, u):
        lines = getattr(u, "ready", [])
        if not lines:
            u.launched = False
            return
        script = []
        for line in lines:
            script.append(
                f"echo '### {line.name} start'; cd {self._home} && "
                f"OS_DY=on OS_NOFPS=on OS_TMP=_t_{line.name} OS_SLAB=2048 "
                f"python3 stitcher.py {line.name}_seg.mp4 0 {round(line.duration, 3)} "
                f"{line.name}.png {self.width} {line.fps:g} 0 1 _t_{line.name} "
                f">> _worker.log 2>&1; rm -rf {self._home}/_t_{line.name}; "
                f"echo '### {line.name} done'")
        cmd = (f"cd {self._home} && nohup bash -c " + json.dumps(" ; ".join(script)) +
               " > _seq.log 2>&1 & sleep 3; pgrep -c -f stitcher.py")
        r = u.exec(cmd, timeout=180)
        u.launched = True
        _log(f"  [{u.label}] worker launched procs={(r.result or '').strip()} lines={len(lines)}")

    def _poll(self, units):
        seen = set()
        pending = {u.label: bool(getattr(u, "ready", [])) for u in units}
        want_total = sum(len(getattr(u, "ready", [])) for u in units)
        while any(pending.values()):
            for u in units:
                if not pending[u.label]:
                    continue
                try:
                    r = u.exec(
                        f"cd {self._home} && ls -la *.png 2>/dev/null | awk '{{print $5,$9}}'; "
                        "echo '@@RUN'; pgrep -c -f stitcher.py || echo 0", timeout=180)
                    body = r.result or ""
                    try:
                        running = int(body.rsplit("@@RUN", 1)[1].strip().splitlines()[-1])
                    except Exception:  # noqa: BLE001
                        running = 0
                    for ln in body.split("@@RUN")[0].strip().splitlines():
                        parts = ln.split()
                        if len(parts) == 2 and parts[1].endswith(".png") and parts[1] != "overview.png":
                            name = parts[1][:-4]
                            if name not in seen and int(parts[0]) > 100_000:
                                self._collect(u, name)
                                seen.add(name)
                    if running == 0 and len(seen) >= want_total:
                        pending[u.label] = False
                    elif running == 0:
                        done = sum(1 for l in getattr(u, "ready", []) if l.name in seen)
                        if done >= len(getattr(u, "ready", [])):
                            pending[u.label] = False
                            _log(f"  [{u.label}] finished")
                except Exception as e:  # noqa: BLE001
                    _log(f"  [{u.label}] poll error {type(e).__name__}")
            if any(pending.values()):
                npass = sum(1 for v in self.state.values() if v.get("verdict") == "PASS")
                _log(f"progress: {npass}/{want_total} | waiting {[k for k, v in pending.items() if v]}")
                time.sleep(60)

    def _collect(self, u, name):
        """Download one finished line's artifacts and run the QC gates."""
        ok_all = True
        for suf in (".png", "_overview.jpg", "_os.json"):
            if not u.download(f"{name}{suf}", os.path.join(self.out, f"{name}{suf}")):
                ok_all = False
        qc = {}
        qp = os.path.join(self.out, f"{name}_os.json")
        if os.path.exists(qp):
            try:
                qc = json.load(open(qp))
            except ValueError:
                qc = {}
        self.qc_by_line[name] = qc
        line = next((l for l in self.cfg.lines if l.name == name), None)
        sharp_src = -1.0
        if line and not line.join:
            sharp_src = qc_mod.source_sharpness(self.src, line.t0, line.t1, width=self.width)
        gate = qc_mod.evaluate(qc, sharp_src) if qc else dict(passed=False, checks=[])
        verdict = "PASS" if (ok_all and gate["passed"]) else "PARTIAL"
        note = ""
        if not gate["passed"] and qc:
            failed = [c["gate"] for c in gate["checks"] if not c["ok"]]
            note = "gate fail: " + ",".join(failed)
        size = f"{qc.get('w')}x{qc.get('h')}" if qc else None
        self.state[name] = dict(verdict=verdict, size=size, note=note, qc=qc,
                                gate=gate["checks"], downloaded=ok_all)
        self.save_state()
        _log(f"  [{u.label}] {name}: {verdict} {size} drift={qc.get('drift_removed')} "
             f"ncc={qc.get('cont_ncc_med')} r={qc.get('cont_r_tx')}"
             + (f" [{note}]" if note else ""))

    # ---------- joins ----------
    def _do_joins(self, joins):
        for line in joins:
            parts = [os.path.join(self.out, f"{p}.png") for p in line.join]
            missing = [p for p in parts if not os.path.exists(p)]
            if missing:
                _log(f"  join {line.name}: missing {missing}")
                continue
            w, h = join_mod.concatenate(parts, os.path.join(self.out, f"{line.name}.png"))
            self.state[line.name] = dict(verdict="PASS", size=f"{w}x{h}",
                                         note="concatenation of " + ",".join(line.join))
            self.save_state()
            _log(f"  join {line.name} -> {w}x{h}")

    # ---------- deliverables ----------
    def _finalize(self):
        rows = report_mod.write_manifest(
            self.out, self.cfg.lines, self.qc_by_line, self.state,
            min_overlap=0.20, min_fps=3.0, norm_height=self.height, source=self.src)
        report_mod.write_contact_sheet(self.out, [l.name for l in self.cfg.lines])
        report_mod.write_readme(self.out, self.cfg.lines, self.state, norm_height=self.height)
        npass = sum(1 for v in self.state.values() if v.get("verdict") == "PASS")
        _log(f"DONE: {npass}/{len(self.cfg.lines)} PASS · deliverables in {self.out}")
        return rows
