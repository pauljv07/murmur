"""Murmur — local AI meeting notes. Run: python server/app.py, then open http://127.0.0.1:8765"""
import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent))
import llm  # noqa: E402
import models  # noqa: E402
import pipeline  # noqa: E402
import store  # noqa: E402
import sysaudio  # noqa: E402
import vocab  # noqa: E402

# Resolve transformers' lazy tokenizer exports once, on the main thread, before any worker
# thread can race on them (see _model_load_lock).
from transformers import AutoTokenizer, PreTrainedTokenizerFast  # noqa: E402,F401

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for noisy in ("nemo_logger", "nemo", "NeMo"):
    logging.getLogger(noisy).setLevel(logging.ERROR)
log = logging.getLogger("murmur")

WEB = Path(__file__).resolve().parent.parent / "web"
app = FastAPI(title="Murmur")
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.middleware("http")
async def revalidate_ui(request, call_next):
    """Browsers must re-check the UI files (cheap 304s via ETag), so an updated app never shows
    a stale cached page."""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

# All model loading happens one at a time: NeMo and mlx-lm both import `transformers`, and
# importing it from two threads at once leaves it half-initialised (ImportError: AutoTokenizer).
_model_load_lock = threading.Lock()

EDITABLE = {"title", "notes", "enhanced", "template", "speakers", "vocabulary"}


def _get(mid: str) -> dict:
    try:
        m = store.get(mid)
    except ValueError:
        m = None
    if m is None:
        raise HTTPException(404, "meeting not found")
    return m


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/status")
def status():
    sel = models.settings()
    names = {k: models.CATALOG[v]["name"] for k, v in (("asr", sel.get("asr")), ("llm", sel.get("llm"))) if v in models.CATALOG}
    return {"models": pipeline.status, "native_audio": sysaudio.available(), "active": names}


# ------------------------------------------------------------------ models (choose / download)

def _load_in_background(kind: str):
    def go():
        with _model_load_lock:
            _load(kind)

    def _load(kind):
        try:
            if kind == "llm":
                pipeline.status["Language model"] = "loading"
                llm.load()
                pipeline.status["Language model"] = "ready"
            else:
                pipeline.load_models()
        except (llm.NoModel, pipeline.ModelMissing) as e:
            log.info("%s", e)
            if kind == "llm":
                pipeline.status["Language model"] = "not installed"
        except Exception:
            log.exception("loading %s failed", kind)
            if kind == "llm":
                pipeline.status["Language model"] = "error"
    threading.Thread(target=go, daemon=True).start()


def _switch_asr() -> bool:
    if not pipeline.gpu_lock.acquire(blocking=False):
        return False
    try:
        pipeline.unload_asr()
    finally:
        pipeline.gpu_lock.release()
    _load_in_background("asr")
    return True


def _download_finished(key: str):
    kind = models.CATALOG[key]["kind"]
    sel = models.settings()
    if kind == "diar" or sel.get(kind) == key:
        if kind == "asr":
            _switch_asr()
        else:
            _load_in_background(kind)


models.downloads.on_done = _download_finished


@app.get("/api/models")
def list_models():
    return models.overview() | {"status": pipeline.status}


@app.post("/api/models/{key}/download")
def download_model(key: str):
    if key not in models.CATALOG:
        raise HTTPException(404, "unknown model")
    need = models.CATALOG[key]["size"] + 1
    if models.system_info()["free_gb"] < need:
        raise HTTPException(507, f"Not enough disk space: {need:.1f} GB needed")
    models.downloads.add(key)
    return {"ok": True}


@app.post("/api/models/{key}/use")
def use_model(key: str):
    m = models.CATALOG.get(key)
    if not m or m["kind"] == "diar":
        raise HTTPException(404, "unknown model")
    if not models.installed(key):
        raise HTTPException(409, "Download this model first")
    if m["kind"] == "asr" and pipeline.gpu_lock.locked():
        raise HTTPException(409, "Stop the recording first")
    models.save_settings(**{m["kind"]: key})
    if m["kind"] == "asr":
        _switch_asr()
    else:
        _load_in_background("llm")
    return models.overview()


@app.delete("/api/models/{key}")
def delete_model(key: str):
    m = models.CATALOG.get(key)
    if not m:
        raise HTTPException(404, "unknown model")
    if m["kind"] == "diar":
        raise HTTPException(409, "Speaker detection is required")
    if models.settings().get(m["kind"]) == key:
        raise HTTPException(409, "This model is in use — choose another one first")
    if (models.downloads.snapshot().get(key) or {}).get("status") in ("queued", "downloading"):
        raise HTTPException(409, "Still downloading")
    models.remove(key)
    return models.overview()


@app.post("/api/onboarding")
def onboarding(body: dict = Body(...)):
    """Welcome screen: save the choices and download whatever is missing."""
    asr, llm_key = body.get("asr"), body.get("llm")
    if asr not in models.CATALOG or models.CATALOG[asr]["kind"] != "asr":
        raise HTTPException(400, "choose a transcription model")
    if llm_key not in models.CATALOG or models.CATALOG[llm_key]["kind"] != "llm":
        raise HTTPException(400, "choose a notes model")
    missing = [k for k in ("diar", asr, llm_key) if not models.installed(k)]
    need = sum(models.CATALOG[k]["size"] for k in missing) + 1
    if missing and models.system_info()["free_gb"] < need:
        raise HTTPException(507, f"Not enough disk space: about {need:.0f} GB needed")
    prev = models.settings()
    models.save_settings(asr=asr, llm=llm_key, onboarded=True)
    for k in missing:
        models.downloads.add(k)
    if asr not in missing and prev.get("asr") != asr:
        _switch_asr()
    else:
        _load_in_background("asr")
    if llm_key not in missing:
        _load_in_background("llm")
    return models.overview()


@app.post("/api/quit")
def quit_app():
    """Stop the local server (the Mac app has no window to close)."""
    if pipeline.gpu_lock.locked():
        raise HTTPException(409, "Stop the recording first")
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return {"ok": True}


@app.get("/api/templates")
def templates():
    return {k: v[0] for k, v in llm.TEMPLATES.items()}


@app.get("/api/meetings")
def list_meetings():
    return store.list_all()


@app.post("/api/meetings")
def create_meeting(body: dict = Body(default={})):
    return store.new_meeting(body.get("title", ""))


@app.get("/api/meetings/{mid}")
def get_meeting(mid: str):
    return _get(mid)


@app.patch("/api/meetings/{mid}")
def patch_meeting(mid: str, body: dict = Body(...)):
    _get(mid)
    return store.update(mid, **{k: v for k, v in body.items() if k in EDITABLE})


@app.delete("/api/meetings/{mid}")
def delete_meeting(mid: str):
    _get(mid)
    store.delete(mid)
    return {"ok": True}


def _sse(gen_fn):
    """Run a blocking text generator in a thread and stream it as server-sent events."""
    try:
        with _model_load_lock:
            llm.load()
    except llm.NoModel as e:
        raise HTTPException(409, str(e))
    q: queue.Queue = queue.Queue()

    def worker():
        try:
            for piece in gen_fn():
                q.put({"text": piece})
        except Exception as e:  # surface model errors to the UI
            log.exception("generation failed")
            q.put({"error": str(e)})
        q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    async def events():
        while True:
            item = await asyncio.to_thread(q.get)
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/meetings/{mid}/enhance")
def enhance(mid: str):
    m = _get(mid)

    def gen():
        out = ""
        for piece in llm.stream(llm.enhance_messages(m), max_tokens=2000):
            pipeline.status["Language model"] = "ready"
            out += piece
            yield piece
        store.update(mid, enhanced=out.strip())
    return _sse(gen)


@app.post("/api/meetings/{mid}/chat")
def chat(mid: str, body: dict = Body(...)):
    m = _get(mid)
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "empty question")

    def gen():
        out = ""
        for piece in llm.stream(llm.chat_messages(m, question), max_tokens=900):
            out += piece
            yield piece
        cur = store.get(mid)
        cur["chat"] = (cur.get("chat", []) + [{"role": "user", "content": question},
                                              {"role": "assistant", "content": out.strip()}])[-20:]
        store.save(cur)
    return _sse(gen)


@app.post("/api/meetings/{mid}/title")
async def make_title(mid: str):
    m = _get(mid)
    if not (m["transcript"] or m["notes"].strip()):
        return {"title": ""}
    def _load():
        with _model_load_lock:
            llm.load()
    try:
        await asyncio.to_thread(_load)
    except llm.NoModel:
        return {"title": ""}
    title = (await asyncio.to_thread(llm.complete, llm.title_messages(m), max_tokens=24, temp=0.2))
    title = title.strip().strip('"').strip().splitlines()[0][:80] if title.strip() else ""
    cur = store.get(mid)
    if title and not cur.get("title"):
        store.update(mid, title=title)
    return {"title": title}


@app.get("/api/vocabulary")
def get_vocabulary():
    return {"text": "\n".join(vocab.read_global())}


@app.put("/api/vocabulary")
def put_vocabulary(body: dict = Body(...)):
    vocab.write_global(body.get("text", ""))
    return {"ok": True}


@app.post("/api/meetings/{mid}/reprocess")
async def reprocess(mid: str):
    m = _get(mid)
    path = store.audio_path(mid)
    if not path.exists():
        raise HTTPException(400, "no recording for this meeting")
    if not pipeline.gpu_lock.acquire(blocking=False):
        raise HTTPException(409, "busy recording or processing")
    try:
        segs, dur = await asyncio.to_thread(pipeline.reprocess, path, m.get("sessions"),
                                            vocab.meeting_terms(m))
    except pipeline.ModelMissing as e:
        raise HTTPException(409, str(e))
    finally:
        pipeline.gpu_lock.release()
    return store.update(mid, transcript=segs, duration=dur)


@app.websocket("/ws/record/{mid}")
async def record(ws: WebSocket, mid: str):
    """Protocol: first a text frame {"type":"start","system":"none"|"browser"|"native"}, then
    binary Float32 16 kHz frames — mono mic, or interleaved [mic, computer] for "browser" —
    and finally {"type":"stop"}. The server pushes {"type":"transcript"} with the full
    transcript whenever it changes, plus "partial", "status" and "error" messages."""
    await ws.accept()
    try:
        m = _get(mid)
    except HTTPException:
        await ws.send_json({"type": "error", "text": "This note no longer exists"})
        await ws.close(code=4404)
        return
    if not pipeline.gpu_lock.acquire(blocking=False):
        await ws.send_json({"type": "error", "text": "Another recording is in progress"})
        await ws.close()
        return

    try:
        first = await asyncio.wait_for(ws.receive(), timeout=15)
        cfg = json.loads(first.get("text") or "{}") if first["type"] == "websocket.receive" else None
    except Exception:
        cfg = None
    if cfg is None:
        pipeline.gpu_lock.release()
        return
    source = cfg.get("system", "none")
    if source not in ("none", "browser", "native"):
        source = "none"
    native = None
    if source == "native":
        if sysaudio.available():
            native = sysaudio.NativeSystemAudio()
        else:
            await ws.send_json({"type": "error", "text": "Native audio helper not built — recording mic only"})
            source = "none"
    dual = source != "none"
    channels = 2 if source == "browser" else 1

    loop = asyncio.get_running_loop()
    inbox: queue.Queue = queue.Queue()
    outbox: asyncio.Queue = asyncio.Queue()
    send = lambda msg: loop.call_soon_threadsafe(outbox.put_nowait, msg)  # noqa: E731

    prior = list(m.get("transcript", []))       # transcript from earlier recording sessions
    offset = float(m.get("duration", 0.0))

    def worker():
        """Owns the LiveSession; all model work happens on this thread."""
        sess = None
        try:
            if any(v != "ready" for k, v in pipeline.status.items() if k != "Language model"):
                send({"type": "status", "text": "Loading speech models (first run downloads them)…"})
            sess = pipeline.LiveSession(dual=dual, time_offset=offset, terms=vocab.meeting_terms(m))
            send({"type": "status", "text": "Listening (mic + computer audio)…" if dual else "Listening…"})
            backlog = np.zeros(0, np.float32)
            warned, last_vocab = False, time.time()
            while True:
                item = inbox.get()
                if item is None:
                    break
                # coalesce whatever arrived while we were busy so we never fall behind
                parts = [item]
                while not inbox.empty():
                    nxt = inbox.get()
                    if nxt is None:
                        inbox.put(None)
                        break
                    parts.append(nxt)
                backlog = np.concatenate([backlog, *parts])
                if len(backlog) < 1600 * channels:
                    continue
                n = len(backlog) // channels * channels
                frame, backlog = backlog[:n], backlog[n:]
                if channels == 2:
                    st = frame.reshape(-1, 2)
                    mic, sysc = st[:, 0].copy(), st[:, 1].copy()
                else:
                    mic = frame
                    sysc = native.take(len(mic)) if native else None
                changed, partial = sess.feed(mic, sysc)
                if changed:
                    _publish(sess)
                if partial is not None:
                    send({"type": "partial", "text": partial})
                if native and not native.alive and not warned:
                    warned = True
                    send({"type": "error", "text": "Computer audio capture stopped. " + (native.error or
                          "Allow Screen & System Audio Recording for your terminal in System Settings.")})
                if time.time() - last_vocab > 15:      # pick up vocabulary edits mid-meeting
                    last_vocab = time.time()
                    cur = store.get(mid)
                    if cur:
                        sess.update_terms(vocab.meeting_terms(cur))
            sess.finish()
            _publish(sess)
        except pipeline.ModelMissing as e:
            send({"type": "error", "text": str(e)})
        except Exception as e:
            log.exception("live pipeline failed")
            send({"type": "error", "text": f"Transcription error: {e}"})
        finally:
            if native:
                native.stop()
            if sess is not None and sess.mic_audio:
                audio2 = sess.audio()
                dur = pipeline.append_wav(store.audio_path(mid), audio2)
                cur = store.get(mid)
                if cur:
                    sessions = cur.get("sessions") or []
                    if not sessions and offset > 0:   # recording made before sessions were tracked
                        sessions.append({"start": 0.0, "end": offset, "dual": False})
                    sessions.append({"start": offset, "end": dur, "dual": dual, "source": source})
                    cur["sessions"], cur["duration"] = sessions, dur
                    store.save(cur)
            send(None)

    def _publish(sess):
        cur = store.get(mid)
        if cur is None:
            return
        cur["transcript"] = prior + sess.segments()
        store.save(cur)
        send({"type": "transcript", "transcript": cur["transcript"]})

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    async def pump_out():
        while True:
            msg = await outbox.get()
            if msg is None:
                break
            try:
                await ws.send_json(msg)
            except Exception:
                pass

    sender = asyncio.create_task(pump_out())
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                inbox.put(np.frombuffer(msg["bytes"], dtype=np.float32).copy())
            elif msg.get("text") and json.loads(msg["text"]).get("type") == "stop":
                break
    except WebSocketDisconnect:
        pass
    finally:
        inbox.put(None)
        await sender
        await asyncio.to_thread(t.join)
        pipeline.gpu_lock.release()
        try:
            await ws.close()
        except Exception:
            pass


@app.on_event("startup")
def warm():
    """Load the selected, installed models in the background so recording starts quickly."""
    def go():
        with _model_load_lock:
            _warm()

    def _warm():
        try:
            pipeline.load_models()
        except pipeline.ModelMissing as e:
            log.info("%s", e)
        except Exception:
            log.exception("speech model warm-up failed")
        try:
            pipeline.status["Language model"] = "loading"
            llm.load()
            pipeline.status["Language model"] = "ready"
        except llm.NoModel:
            pipeline.status["Language model"] = "not installed"
        except Exception:
            pipeline.status["Language model"] = "error"
            log.exception("language model warm-up failed")
    threading.Thread(target=go, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("MURMUR_HOST", "127.0.0.1"),
                port=int(os.environ.get("MURMUR_PORT", "8765")), log_level="warning")
