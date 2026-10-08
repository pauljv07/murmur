"""Murmur — local AI meeting notes. Run: python server/app.py, then open http://127.0.0.1:8765"""
import asyncio
import json
import logging
import os
import queue
import sys
import threading
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent))
import llm  # noqa: E402
import pipeline  # noqa: E402
import store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for noisy in ("nemo_logger", "nemo", "NeMo"):
    logging.getLogger(noisy).setLevel(logging.ERROR)
log = logging.getLogger("murmur")

WEB = Path(__file__).resolve().parent.parent / "web"
app = FastAPI(title="Murmur")
app.mount("/static", StaticFiles(directory=WEB), name="static")

EDITABLE = {"title", "notes", "enhanced", "template", "speakers"}


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
    return {"models": pipeline.status}


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
        pipeline.status["Language model"] = "ready" if llm._model else "loading"
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
    title = (await asyncio.to_thread(llm.complete, llm.title_messages(m), max_tokens=24, temp=0.2))
    title = title.strip().strip('"').strip().splitlines()[0][:80] if title.strip() else ""
    cur = store.get(mid)
    if title and not cur.get("title"):
        store.update(mid, title=title)
    return {"title": title}


@app.post("/api/meetings/{mid}/reprocess")
async def reprocess(mid: str):
    _get(mid)
    path = store.audio_path(mid)
    if not path.exists():
        raise HTTPException(400, "no recording for this meeting")
    if not pipeline.gpu_lock.acquire(blocking=False):
        raise HTTPException(409, "busy recording or processing")
    try:
        segs, dur = await asyncio.to_thread(pipeline.reprocess, path)
    finally:
        pipeline.gpu_lock.release()
    return store.update(mid, transcript=segs, duration=dur)


@app.websocket("/ws/record/{mid}")
async def record(ws: WebSocket, mid: str):
    await ws.accept()
    try:
        m = _get(mid)
    except HTTPException:
        await ws.close(code=4404)
        return
    if not pipeline.gpu_lock.acquire(blocking=False):
        await ws.send_json({"type": "error", "text": "Another recording is in progress"})
        await ws.close()
        return

    loop = asyncio.get_running_loop()
    inbox: queue.Queue = queue.Queue()
    outbox: asyncio.Queue = asyncio.Queue()
    send = lambda msg: loop.call_soon_threadsafe(outbox.put_nowait, msg)  # noqa: E731

    def worker():
        """Owns the LiveSession; all model work happens on this thread."""
        sess = None
        try:
            if any(v != "ready" for k, v in pipeline.status.items() if k != "Language model"):
                send({"type": "status", "text": "Loading speech models (first run downloads them)…"})
            sess = pipeline.LiveSession(time_offset=m.get("duration", 0.0))
            send({"type": "status", "text": "Listening…"})
            backlog = np.zeros(0, np.float32)
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
                if len(backlog) < 1600:
                    continue
                segs, partial = sess.feed(backlog)
                backlog = np.zeros(0, np.float32)
                _publish(segs)
                if partial is not None:
                    send({"type": "partial", "text": partial})
            if len(backlog):
                _publish(sess.feed(backlog)[0])
            _publish(sess.finish())
        except Exception as e:
            log.exception("live pipeline failed")
            send({"type": "error", "text": f"Transcription error: {e}"})
        finally:
            if sess is not None and len(sess.audio):
                dur = pipeline.append_wav(store.audio_path(mid), sess.session_audio())
                store.update(mid, duration=dur)
            send(None)

    def _publish(segs):
        if not segs:
            return
        cur = store.get(mid)
        tr = cur["transcript"]
        for s in segs:
            last = tr[-1] if tr else None
            if last and last["speaker"] == s["speaker"] and s["start"] - last["end"] < 2:
                last["text"] += " " + s["text"]
                last["end"] = s["end"]
            else:
                tr.append(dict(s))
            send({"type": "segment", "segment": s})
        store.save(cur)

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
    """Load models in the background so the first recording starts quickly."""
    def go():
        try:
            pipeline.load_models()
            pipeline.status["Language model"] = "loading"
            llm.load()
            pipeline.status["Language model"] = "ready"
        except Exception:
            log.exception("model warm-up failed")
    threading.Thread(target=go, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("MURMUR_HOST", "127.0.0.1"),
                port=int(os.environ.get("MURMUR_PORT", "8765")), log_level="warning")
