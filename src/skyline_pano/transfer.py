"""transfer.py — robust chunked upload/download for Daytona sandboxes.

The SDK's single-shot ``upload_file`` times out on files above ~100 MB, and both upload
and download can hang indefinitely with no timeout. So: split large files into parts,
upload them with a thread pool (the local uplink is shared, so N concurrent streams reach
the real link speed), ``cat`` them back together inside the sandbox and verify the size;
every attempt is bounded by a daemon thread so a hang is abandoned and retried.
"""
import os
import time
import threading
import queue

REMOTE_HOME = "/home/daytona"


def _direct(sb, local, remote, tries=5, timeout=180):
    """One file, retried; each attempt bounded by a thread (the SDK can hang)."""
    for a in range(tries):
        box = {}

        def do():
            try:
                sb.fs.upload_file(local, f"{REMOTE_HOME}/{remote}")
                box["ok"] = True
            except Exception as e:  # noqa: BLE001 - report any SDK failure
                box["err"] = e

        t = threading.Thread(target=do, daemon=True)
        t.start()
        t.join(timeout)
        if box.get("ok"):
            return True
        if not t.is_alive():
            box.get("err")
        time.sleep(3 + 6 * a)
    return False


def remote_size(sb, remote):
    r = sb.process.exec(f"stat -c%s {REMOTE_HOME}/{remote} 2>/dev/null", timeout=120)
    return (r.result or "").strip()


def upload(sb, local, remote, part_mb=8, workers=8, tries=5, verify=True):
    """Upload ``local`` to the sandbox. Returns (ok, MB/s)."""
    size = os.path.getsize(local)
    lim = part_mb * 1024 * 1024
    t0 = time.time()
    if size <= lim:
        ok = _direct(sb, local, remote, tries)
        return ok, size / 1e6 / max(1e-3, time.time() - t0)

    parts = []
    with open(local, "rb") as f:
        i = 0
        while True:
            data = f.read(lim)
            if not data:
                break
            p = f"{local}.p{i:03d}"
            with open(p, "wb") as g:
                g.write(data)
            parts.append((p, f"{remote}.p{i:03d}"))
            i += 1

    q = queue.Queue()
    for item in parts:
        q.put(item)
    results = {}
    lock = threading.Lock()

    def worker():
        while True:
            try:
                p, r = q.get_nowait()
            except queue.Empty:
                return
            ok = _direct(sb, p, r, tries)
            with lock:
                results[r] = ok

    ths = [threading.Thread(target=worker) for _ in range(min(workers, len(parts)))]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    ok = all(results.values())
    if ok:
        r = sb.process.exec(
            f"cd {REMOTE_HOME} && cat {remote}.p* > {remote} && rm -f {remote}.p* && "
            f"stat -c%s {remote}", timeout=900)
        got = (r.result or "").strip().splitlines()[-1] if r.result else "?"
        if verify:
            try:
                ok = int(got) == size
            except Exception:  # noqa: BLE001
                ok = False
    for p, _ in parts:
        try:
            os.remove(p)
        except OSError:
            pass
    return ok, size / 1e6 / max(1e-3, time.time() - t0)


def download(sb, remote, local, tries=5, timeout=300):
    """Download one file; each attempt bounded by a thread (the SDK can hang)."""
    for a in range(tries):
        box = {}

        def do():
            try:
                data = sb.fs.download_file(f"{REMOTE_HOME}/{remote}")
                if data:
                    box["data"] = data
            except Exception as e:  # noqa: BLE001
                box["err"] = e

        t = threading.Thread(target=do, daemon=True)
        t.start()
        t.join(timeout)
        data = box.get("data")
        if data:
            os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
            with open(local, "wb") as f:
                f.write(data)
            return True
        time.sleep(3 + 6 * a)
    return False
