"""JSON-file meeting store. One file per meeting under data/meetings/."""
import json
import threading
import time
import uuid
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
MEETINGS = DATA / "meetings"
AUDIO = DATA / "audio"
MEETINGS.mkdir(parents=True, exist_ok=True)
AUDIO.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


def _path(mid: str) -> Path:
    if not mid.replace("-", "").isalnum():
        raise ValueError("bad id")
    return MEETINGS / f"{mid}.json"


def new_meeting(title: str = "") -> dict:
    m = {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "created": time.time(),
        "updated": time.time(),
        "notes": "",          # what the user typed during the meeting
        "enhanced": "",       # AI-written notes (markdown)
        "template": "general",
        "transcript": [],     # [{speaker, start, end, text}]
        "speakers": {},       # "0" -> "Alice"
        "duration": 0.0,
        "chat": [],           # [{role, content}]
        "vocabulary": "",     # names / jargon for this meeting (comma or newline separated)
        "sessions": [],       # [{start, end, dual, source}] one per recording session
    }
    save(m)
    return m


def get(mid: str) -> dict | None:
    p = _path(mid)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def save(m: dict) -> None:
    m["updated"] = time.time()
    with _lock:
        tmp = _path(m["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=1))
        tmp.replace(_path(m["id"]))


def update(mid: str, **fields) -> dict | None:
    with _lock:
        m = get(mid)
    if m is None:
        return None
    m.update(fields)
    save(m)
    return m


def delete(mid: str) -> None:
    _path(mid).unlink(missing_ok=True)
    (AUDIO / f"{mid}.wav").unlink(missing_ok=True)


def list_all() -> list[dict]:
    out = []
    for p in MEETINGS.glob("*.json"):
        try:
            m = json.loads(p.read_text())
        except Exception:
            continue
        out.append({k: m.get(k) for k in ("id", "title", "created", "updated", "duration")}
                   | {"has_notes": bool(m.get("enhanced") or m.get("notes"))})
    return sorted(out, key=lambda x: x["created"], reverse=True)


def audio_path(mid: str) -> Path:
    _path(mid)  # validates id
    return AUDIO / f"{mid}.wav"
