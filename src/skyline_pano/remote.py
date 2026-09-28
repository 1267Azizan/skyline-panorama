"""remote.py — a pool of hosted sandboxes, provider-neutral.

This module drives any sandbox provider whose Python SDK exposes the small surface used
below. The provider is described by a JSON file (``sandbox.json`` by default; copy
``sandbox.example.json``) so no provider name is baked into the code:

    {
      "sdk": "your_provider_sdk",                # importable SDK module
      "client_class": "Client",                  # class that talks to the service
      "params_class": "CreateSandboxParams",     # kwargs object passed to create()
      "key_env": "SKYLINE_SANDBOX_KEY",          # env var the SDK reads the key from
      "snapshot": "your-snapshot-name",          # pre-built image to boot
      "auto_stop_interval": 0,
      "keys": ["<key1>", "<key2>"]               # one per account (optional)
    }

Environment variables ``SKYLINE_SANDBOX_KEY``, ``SKYLINE_SANDBOX_KEY_2``, … override the
file's keys. Accounts are isolated and each carries its own quota, so more accounts means
more parallelism: the pool boots ONE sandbox per account and balances the longest lines
first. Keys are never logged.

Required SDK surface:
    client = <client_class>()                          # reads the key from <key_env>
    params = <params_class>(snapshot=..., auto_stop_interval=...)
    sb = client.create(params, timeout=...)            # also: client.get(id), client.delete(sb)
    sb.process.exec(cmd, timeout=...)                  # -> .result (stdout text)
    sb.fs.upload_file(local_path, remote_path)
    sb.fs.download_file(remote_path)                   # -> bytes
"""
import os
import re
import json
import time
import importlib

KEY_RE = re.compile(r"^SKYLINE_SANDBOX_KEY(_\d+)?$")
DEFAULT_CONFIG = "sandbox.json"


def load_config(path=None):
    """Read the provider config; environment keys override the file's keys."""
    path = path or os.environ.get("SKYLINE_SANDBOX_CONFIG") or DEFAULT_CONFIG
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"sandbox provider config not found: {path}\n"
            f"copy sandbox.example.json to {DEFAULT_CONFIG} and fill it in")
    cfg = json.load(open(path, encoding="utf-8"))
    for field in ("sdk", "client_class", "params_class", "key_env"):
        if not cfg.get(field):
            raise ValueError(f"sandbox config missing '{field}': {path}")
    # collect keys from the environment: the generic SKYLINE_SANDBOX_KEY family AND the
    # provider's own key_env (so a machine that already exports that variable just works).
    # First match wins; duplicates collapse.
    names = [k for k in sorted(os.environ) if KEY_RE.match(k) and os.environ[k]]
    provider_env = cfg["key_env"]
    if os.environ.get(provider_env):
        names.insert(0, provider_env)
        names += [f"{provider_env}_{i}" for i in range(2, 21) if os.environ.get(f"{provider_env}_{i}")]
    env_keys = []
    for n in names:
        v = os.environ.get(n)
        if v and v not in env_keys:
            env_keys.append(v)
    if env_keys:
        cfg["keys"] = env_keys
    cfg.setdefault("keys", [])
    cfg.setdefault("auto_stop_interval", 0)
    cfg["_path"] = os.path.abspath(path)
    return cfg


def _load_sdk(cfg):
    """Import the provider SDK and return (Client, Params, key_env)."""
    sdk = importlib.import_module(cfg["sdk"])
    client_cls = getattr(sdk, cfg["client_class"])
    params_cls = getattr(sdk, cfg["params_class"])
    return client_cls, params_cls


class Sandbox:
    """One running sandbox plus the account it belongs to."""

    def __init__(self, label, key, client, sb, cfg, log=print):
        self.label = label
        self.key = key
        self.client = client
        self.sb = sb
        self.cfg = cfg
        self.log = log
        self.lines = []
        self.home = cfg.get("remote_home") or "/home/user"

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
        kw.setdefault("home", self.home)
        return transfer.upload(self.sb, local, remote, **kw)

    def download(self, remote, local, **kw):
        from . import transfer  # noqa: PLC0415
        kw.setdefault("home", self.home)
        return transfer.download(self.sb, remote, local, **kw)

    def delete(self):
        self.client.delete(self.sb)


class Pool:
    """Create/reuse/cleanup one sandbox per account."""

    def __init__(self, config_path=None, max_accounts=3, log=print):
        self.cfg = load_config(config_path)
        self.max_accounts = max_accounts
        self.log = log
        self.units = []

    # ---------- lifecycle ----------
    def up(self, reuse=True):
        keys = self.cfg["keys"][: self.max_accounts]
        if not keys:
            raise RuntimeError("no sandbox API key configured (sandbox.json or "
                               "SKYLINE_SANDBOX_KEY)")
        for i, key in enumerate(keys):
            label = f"account{i + 1}"
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
        client_cls, params_cls = _load_sdk(self.cfg)
        os.environ[self.cfg["key_env"]] = key
        client = client_cls()
        sb = None
        if reuse:
            for it in self._list(key):
                if (it.get("state") or "") == "started":
                    try:
                        sb = client.get(it["id"])
                        break
                    except Exception:  # noqa: BLE001
                        pass
            for it in self._list(key):
                if (it.get("state") or "") in ("error", "creating", "archiving"):
                    self._delete_rest(key, it["id"])
        if sb is None:
            for attempt in range(2):
                try:
                    sb = client.create(params_cls(snapshot=self.cfg["snapshot"],
                                                  auto_stop_interval=self.cfg["auto_stop_interval"]),
                                       timeout=420)
                    break
                except Exception as e:  # noqa: BLE001
                    self.log(f"  [{label}] create try{attempt + 1}: {type(e).__name__} {str(e)[:80]}")
                    time.sleep(15)
        if sb is None:
            return None
        return Sandbox(label, key, client, sb, self.cfg, self.log)

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
                self.log(f"  released {u.id} ({u.label})")
            except Exception as e:  # noqa: BLE001
                self.log(f"  release failed {u.id}: {type(e).__name__}")
        self.units = []

    # ---------- REST helpers (the SDK's list() has been unreliable) ----------
    @staticmethod
    def _api_base(cfg):
        return cfg.get("api_base", "").rstrip("/")

    def _list(self, key):
        import urllib.request  # noqa: PLC0415
        import urllib.error  # noqa: PLC0415
        base = self._api_base(self.cfg)
        if not base:
            return []
        req = urllib.request.Request(f"{base}/sandbox",
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"})
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

    def _delete_rest(self, key, sid):
        import urllib.request  # noqa: PLC0415
        import urllib.error  # noqa: PLC0415
        base = self._api_base(self.cfg)
        if not base:
            return
        req = urllib.request.Request(f"{base}/sandbox/{sid}", method="DELETE",
                                     headers={"Authorization": f"Bearer {key}"})
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except (urllib.error.URLError, ValueError):
            pass
