"""Model catalog, user choice, download manager and system info.

Users pick models in the app (welcome screen / Models dialog). Downloads run in a subprocess
with network access enabled; everything else in the server loads strictly from the local cache
so Murmur works offline once the chosen models are on disk.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import store

SETTINGS = store.DATA / "settings.json"

# kind: asr | diar | llm.  files: allow-patterns to download (None = whole repo).
# ram: GB of memory recommended for the whole app with this model.
CATALOG = {
    "asr-multi": dict(kind="asr", repo="nvidia/parakeet-tdt-0.6b-v3", files=["*.nemo"], size=2.51,
                      name="Multilingual", desc="English + 24 European languages (detected automatically)",
                      tag="NVIDIA Parakeet TDT 0.6B v3"),
    "asr-en": dict(kind="asr", repo="nvidia/parakeet-tdt-0.6b-v2", files=["*.nemo"], size=2.47,
                   name="English only", desc="Tuned for English meetings",
                   tag="NVIDIA Parakeet TDT 0.6B v2"),
    "diar": dict(kind="diar", repo="nvidia/diar_streaming_sortformer_4spk-v2.1", files=["*.nemo"], size=0.47,
                 name="Speaker detection", desc="Tells up to 4 voices apart",
                 tag="NVIDIA Streaming Sortformer v2.1"),
    "llm-1.7b": dict(kind="llm", repo="mlx-community/Qwen3-1.7B-4bit", files=None, size=0.98, ram=8,
                     name="Light", desc="Smallest and quickest; simpler notes", tag="Qwen3 1.7B",
                     speed=3, quality=1),
    "llm-4b": dict(kind="llm", repo="mlx-community/Qwen3-4B-Instruct-2507-4bit", files=None, size=2.28, ram=12,
                   name="Fast", desc="Quick, good notes", tag="Qwen3 4B Instruct", speed=3, quality=2),
    "llm-8b": dict(kind="llm", repo="mlx-community/Qwen3-8B-4bit", files=None, size=4.62, ram=16,
                   name="Balanced", desc="Better notes, fewer mistakes", tag="Qwen3 8B", speed=2, quality=3),
    "llm-14b": dict(kind="llm", repo="mlx-community/Qwen3-14B-4bit", files=None, size=8.32, ram=32,
                    name="Best", desc="Most careful notes; slower", tag="Qwen3 14B", speed=1, quality=4),
}
DEFAULTS = {"asr": "asr-multi", "llm": None}


# ------------------------------------------------------------------ settings

def settings() -> dict:
    try:
        s = json.loads(SETTINGS.read_text())
    except Exception:
        s = {}
    return {**DEFAULTS, **s}


def save_settings(**kw):
    s = settings()
    s.update(kw)
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, indent=1))
    return s


def repo_for(kind: str) -> str | None:
    """The repo to load for asr / diar / llm. Env vars still override (developer use)."""
    env = {"asr": "MURMUR_ASR_MODEL", "diar": "MURMUR_DIAR_MODEL", "llm": "MURMUR_LLM"}[kind]
    if os.environ.get(env):
        return os.environ[env]
    if kind == "diar":
        return CATALOG["diar"]["repo"]
    key = settings().get(kind)
    return CATALOG[key]["repo"] if key in CATALOG else None


# ------------------------------------------------------------------ cache inspection

def _hub() -> Path:
    from huggingface_hub import constants
    return Path(constants.HF_HUB_CACHE)


def _snapshot(repo: str) -> Path | None:
    d = _hub() / ("models--" + repo.replace("/", "--"))
    ref = d / "refs" / "main"
    if ref.exists():
        snap = d / "snapshots" / ref.read_text().strip()
        if snap.exists():
            return snap
    snaps = sorted((d / "snapshots").glob("*"), key=lambda p: p.stat().st_mtime) if (d / "snapshots").exists() else []
    return snaps[-1] if snaps else None


def installed(key: str) -> bool:
    m = CATALOG[key]
    snap = _snapshot(m["repo"])
    if snap is None:
        return False
    if m["files"] == ["*.nemo"]:
        return any(p.exists() for p in snap.glob("*.nemo"))          # exists() follows the link
    # MLX: config, tokenizer and every weight shard must be present
    if not (snap / "config.json").exists():
        return False
    if not any((snap / t).exists() for t in ("tokenizer.json", "tokenizer.model")):
        return False
    idx = snap / "model.safetensors.index.json"
    if idx.exists():
        shards = set(json.loads(idx.read_text()).get("weight_map", {}).values())
        return all((snap / s).exists() for s in shards)
    return (snap / "model.safetensors").exists()


def remove(key: str):
    """Delete a model with huggingface_hub's own cache API (it understands shared blobs)."""
    from huggingface_hub import scan_cache_dir
    repo = CATALOG[key]["repo"]
    info = scan_cache_dir(_hub())
    revs = [r.commit_hash for rp in info.repos if rp.repo_id == repo for r in rp.revisions]
    if revs:
        info.delete_revisions(*revs).execute()
    d = _hub() / ("models--" + repo.replace("/", "--"))
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ system

def system_info() -> dict:
    try:
        ram = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip()) / 2**30
        chip = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"]).decode().strip()
    except Exception:
        ram, chip = 0, ""
    hub = _hub()
    hub.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(hub).free / 1e9
    return {"ram_gb": round(ram), "chip": chip, "free_gb": round(free, 1)}


def recommended_llm(ram_gb: float) -> str:
    if ram_gb >= 32:
        return "llm-14b"
    if ram_gb >= 16:
        return "llm-8b"
    if ram_gb >= 12:
        return "llm-4b"
    return "llm-1.7b"


# ------------------------------------------------------------------ downloads

DOWNLOAD = r'''
import sys, threading, time
from pathlib import Path
from huggingface_hub import HfApi, snapshot_download, constants
from fnmatch import fnmatch
repo, pats = sys.argv[1], (sys.argv[2:] or None)
info = HfApi().model_info(repo, files_metadata=True)
want = [s for s in info.siblings if not pats or any(fnmatch(s.rfilename, p) for p in pats)]
total = sum((s.size or 0) for s in want) or 1
hub = Path(constants.HF_HUB_CACHE)
def measure():
    # bytes on disk in the cache store (incl. partial downloads); shared Xet store lives in hub/blobs
    n = 0
    for d in (hub / ("models--" + repo.replace("/", "--")) / "blobs", hub / "blobs"):
        if d.exists():
            for f in d.rglob("*"):
                try:
                    if f.is_file() and not f.is_symlink():
                        n += f.stat().st_size
                except OSError:
                    pass
    return n
start = measure()
done = False
def report():
    while not done:
        print(f"PROGRESS {min(max(measure() - start, 0), total)} {total}", flush=True)
        time.sleep(1)
threading.Thread(target=report, daemon=True).start()
snapshot_download(repo, allow_patterns=pats)
done = True
print(f"PROGRESS {total} {total}", flush=True)
'''


class Downloads:
    """Sequential background download queue with per-model progress."""

    def __init__(self):
        self.lock = threading.Lock()
        self.queue: list[str] = []
        self.state: dict[str, dict] = {}     # key -> {status, done, total, error}
        self.on_done = None                  # callback(key)
        self._thread = None

    def add(self, key: str):
        with self.lock:
            if key in self.queue or self.state.get(key, {}).get("status") == "downloading":
                return
            self.queue.append(key)
            self.state[key] = {"status": "queued", "done": 0, "total": int(CATALOG[key]["size"] * 1e9), "error": None}
            if not self._thread or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def _run(self):
        while True:
            with self.lock:
                if not self.queue:
                    return
                key = self.queue.pop(0)
                st = self.state[key]
                st["status"] = "downloading"
            m = CATALOG[key]
            env = {**os.environ, "HF_HUB_OFFLINE": "0", "TRANSFORMERS_OFFLINE": "0",
                   "HF_HUB_DISABLE_PROGRESS_BARS": "1"}
            try:
                p = subprocess.Popen([sys.executable, "-c", DOWNLOAD, m["repo"], *(m["files"] or [])],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
                tail = []
                for line in p.stdout:
                    if line.startswith("PROGRESS "):
                        a, b = line.split()[1:3]
                        st["done"], st["total"] = int(a), int(b)
                    else:
                        tail = (tail + [line.strip()])[-5:]
                if p.wait() != 0 or not installed(key):
                    msg = next((t for t in reversed(tail) if t), "download failed")
                    if "ConnectionError" in msg or "resolve" in msg.lower() or "offline" in msg.lower():
                        msg = "No internet connection — connect and try again."
                    raise RuntimeError(msg[-200:])
                st["status"], st["done"] = "done", st["total"]
                if self.on_done:
                    self.on_done(key)
            except Exception as e:
                st["status"], st["error"] = "error", str(e)

    def snapshot(self) -> dict:
        with self.lock:
            return {k: dict(v) for k, v in self.state.items()}


downloads = Downloads()


def overview() -> dict:
    sysinfo = system_info()
    s = settings()
    dl = downloads.snapshot()
    items = []
    for key, m in CATALOG.items():
        items.append({"key": key, **{k: v for k, v in m.items() if k != "files"},
                      "installed": installed(key), "selected": (s.get(m["kind"]) == key) or m["kind"] == "diar",
                      "download": dl.get(key)})
    return {"models": items, "selected": {"asr": s.get("asr"), "llm": s.get("llm")},
            "system": sysinfo, "recommended": {"asr": "asr-multi", "llm": recommended_llm(sysinfo["ram_gb"])},
            "onboarded": bool(s.get("onboarded"))}
