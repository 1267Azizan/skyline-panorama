"""daytona.py — a pool of Daytona sandboxes spread across every configured account.

Daytona accounts are isolated and each carries its own quota, so the fastest way to get
more parallelism is more accounts. Tier-1 gives 10 vCPU / 10 GiB per account and a
4 vCPU / 8 GiB ceiling per sandbox, so we run ONE ``daytona-large`` sandbox per account
and balance the longest lines first.

Keys are read from the environment (``DAYTONA_API_KEY``, ``DAYTONA_API_KEY_2``, …) and/or
a ``_dt_keys.json`` file next to the project (``{"keys": ["dtn_...", ...]}``). Keys are
never logged.
"""
import os
import json
import time
import sys


def _env_keys():
    keys = []
    for name in ["DAYTONA_API_KEY"] + [f"DAYTONA_API_KEY_{i}" for i in range(2, 21)]:
        v = os.environ.get(name)
        if v:
            keys.append((name, v.strip()))
    return keys


def _file_keys():
    out = []
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for path in (os.path.join(here, "_dt_keys.json"),
                 os.path.expanduser("~/AppData/Local/hermes/.env")):
        if path.endswith(".json") and os.path.exists(path):
            try:
                for i, k in enumerate(json.load(open(path)).get("keys", [])):
                    if k:
                        out.append((f"file{i + 1}", k.strip()))
            except Exception:  # noqa: BLE001
                pass
        elif os.path.exists(path):
            for line in open(path, encoding="utf-8", errors="ignore"):
                if line.strip().startswith("DAYTONA_API_KEY="):
                    out.append(("envfile", line.split("=", 1)[1].strip().strip('"').strip("'")))
    return out


def all_keys():
    """Every unique (label, key) we can find, in priority order."""
    seen, keys = set(), []
    for label, k in _env_keys() + _file_keys():
        if k and k not in seen:
            seen.add(k)
            keys.append((label, k))
    return keys


def _fresh_sdk(key):
    """Import the Daytona SDK against a specific key.

    The SDK reads DAYTONA_API_KEY at import time, so switching accounts means setting the
    env var and dropping the cached modules.
    """
    os.environ["DAYTONA_API_KEY"] = key
    for m in list(sys.modules):
        if m.startswith("daytona"):
            del sys.modules[m]
    from daytona import Daytona, CreateSandboxFromSnapshotParams  # noqa: PLC0415
    return Daytona(), CreateSandboxFromSnapshotParams


class Sandbox:
    """One running sandbox plus the account it belongs to."""

    def __init__(self, label, key, client, sandbox):
        self.label = label
        self.key = key
        self.client = client
        self.sb = sandbox
        self.lines = []

    @property
    def id(self):
        return self.sb.id

    def exec(self, cmd, timeout=180):
        return self.sb.process.exec(cmd, timeout=timeout)

    def install_deps(self, packages=("opencv-python-headless", "numpy", "pillow"), timeout=1800):
        pkgs = " ".join(packages)
        self.sb.process.exec(f"pip install -q --no-input {pkgs} 2>&1 | tail -1", timeout=timeout)

    def upload(self, local, remote, **kw):
        from . import transfer  # noqa: PLC0415
        return transfer.upload(self.sb, local, remote, **kw)

    def download(self, remote, local, **kw):
        from . import transfer  # noqa: PLC0415
        return transfer.download(self.sb, remote, local, **kw)

    def delete(self):
        self.client.delete(self.sb)


class Pool:
    """Create/reuse/cleanup one sandbox per account."""

    def __init__(self, snapshot="daytona-large", max_accounts=3, log=print):
        self.snapshot = snapshot
        self.max_accounts = max_accounts
        self.log = log
        self.units = []

    def up(self, reuse=True):
        keys = all_keys()[: self.max_accounts]
        if not keys:
            raise RuntimeError("no Daytona API key found (set DAYTONA_API_KEY)")
        for label, key in keys:
            try:
                unit = self._one(label, key, reuse)
            except Exception as e:  # noqa: BLE001
                self.log(f"  [{label}] unavailable: {type(e).__name__} {str(e)[:90]}")
                continue
            if unit:
                self.units.append(unit)
                self.log(f"  [{label}] sandbox {unit.id}")
        if not self.units:
            raise RuntimeError("no sandbox could be created")
        return self.units

    def _one(self, label, key, reuse):
        client, params = _fresh_sdk(key)
        sb = None
        if reuse:
            for it in self._list(key):
                if (it.get("state") or "") == "started":
                    try:
                        sb = client.get(it["id"])
                        break
                    except Exception:  # noqa: BLE001
                        pass
            # clear broken leftovers that only hold quota
            for it in self._list(key):
                if (it.get("state") or "") in ("error", "creating", "archiving"):
                    self._delete_rest(key, it["id"])
        if sb is None:
            for attempt in range(2):
                try:
                    sb = client.create(params(snapshot=self.snapshot, auto_stop_interval=0),
                                       timeout=420)
                    break
                except Exception as e:  # noqa: BLE001
                    self.log(f"  [{label}] create try{attempt + 1}: {type(e).__name__} {str(e)[:80]}")
                    time.sleep(15)
        if sb is None:
            return None
        return Sandbox(label, key, client, sb)

    @staticmethod
    def _list(key):
        """REST listing (the SDK's list() has been unreliable)."""
        import urllib.request  # noqa: PLC0415
        import urllib.error  # noqa: PLC0415
        req = urllib.request.Request(
            "https://app.daytona.io/api/sandbox",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode() or "{}")
        except (urllib.error.URLError, ValueError):
            return []
        if isinstance(body, list):
            return body
        for k in ("items", "data", "sandboxes"):
            if isinstance(body.get(k), list):
                return body[k]
        return []

    @staticmethod
    def _delete_rest(key, sid):
        import urllib.request  # noqa: PLC0415
        import urllib.error  # noqa: PLC0415
        req = urllib.request.Request(
            f"https://app.daytona.io/api/sandbox/{sid}", method="DELETE",
            headers={"Authorization": f"Bearer {key}"})
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except (urllib.error.URLError, ValueError):
            pass

    def balance(self, lines):
        """Assign lines to units, longest first, round-robin."""
        for u in self.units:
            u.lines = []
        for i, line in enumerate(sorted(lines, key=lambda l: -l.duration)):
            self.units[i % len(self.units)].lines.append(line)
        return self.units

    def down(self):
        for u in self.units:
            try:
                u.delete()
                self.log(f"  deleted {u.id} ({u.label})")
            except Exception as e:  # noqa: BLE001
                self.log(f"  delete failed {u.id}: {type(e).__name__}")
        self.units = []
