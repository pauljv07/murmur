"""Murmur first-run setup + launcher (standard library only).

Started by the Mac app with a uv-managed Python 3.11. On first launch it serves a small
progress page on the app's port while it creates a private virtualenv from the pinned
requirements, then replaces itself with the real server on the same port, so the browser page
turns into the app — where the user picks and downloads models on the welcome screen.
"""
import hashlib
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

RES = Path(__file__).resolve().parent                       # .app/Contents/Resources
SUPPORT = Path(os.environ["MURMUR_SUPPORT"])                 # ~/Library/Application Support/Murmur
VENV = SUPPORT / "venv"
PY = VENV / "bin" / "python"
PORT = int(os.environ.get("MURMUR_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/"
UV = RES / "uv"
LOCK = RES / "requirements.lock"

state = {"step": "Starting…", "detail": "", "progress": None, "error": None, "log": []}


def log(msg: str):
    print(msg, flush=True)
    state["log"] = (state["log"] + [msg])[-12:]


def lock_hash() -> str:
    return hashlib.sha256(LOCK.read_bytes()).hexdigest()[:16]


# ------------------------------------------------------------------ progress page

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Setting up Murmur</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#fbfaf7;--ink:#1d1c1a;--muted:#8a867d;--line:#e4e0d7;--accent:#3d7a4f;--err:#b4432f}
@media (prefers-color-scheme:dark){:root{--bg:#1a1a18;--ink:#ecebe6;--muted:#9a968c;--line:#34332f;--accent:#6fbf86}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,sans-serif}
main{max-width:520px;margin:16vh auto 0;padding:0 16px}
h1{font:500 28px "Iowan Old Style",Georgia,serif;margin:0 0 6px}
.bar{height:8px;border-radius:99px;background:var(--line);overflow:hidden;margin:18px 0 8px}
.bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .6s}
.bar.indef i{width:30%;animation:slide 1.4s infinite ease-in-out}
@keyframes slide{0%{margin-left:-30%}100%{margin-left:100%}}
#step{font-weight:600}#detail,.note{color:var(--muted);font-size:13px}
pre{font-size:11px;color:var(--muted);white-space:pre-wrap;margin-top:24px;max-height:30vh;overflow:auto}
.err{color:var(--err)}
</style></head><body><main>
<h1>Setting up Murmur</h1>
<p class="note">First launch only: installing Murmur's components (about 2 GB, a few minutes).
Next you'll choose your AI models. Everything runs on this Mac — nothing you record is uploaded.</p>
<div class="bar indef" id="bar"><i id="fill"></i></div>
<div id="step">Starting…</div><div id="detail"></div><pre id="log"></pre>
</main><script>
async function poll(){
  try{
    const r=await fetch("/setup/status",{cache:"no-store"});
    if(!r.ok) throw 0;
    const s=await r.json();
    document.getElementById("step").textContent=s.error?"Setup failed":s.step;
    document.getElementById("step").className=s.error?"err":"";
    document.getElementById("detail").textContent=s.error||s.detail;
    document.getElementById("log").textContent=s.log.join("\\n");
    const bar=document.getElementById("bar");
    bar.classList.toggle("indef",s.progress==null&&!s.error);
    if(s.progress!=null) document.getElementById("fill").style.width=(s.progress*100).toFixed(1)+"%";
    if(s.ready){ setTimeout(()=>location.replace("/"),1500); return; }
    if(s.error) return;
  }catch(e){
    // the setup server hands over to the app: once the app answers, go there
    try{ const a=await fetch("/api/status",{cache:"no-store"}); if(a.ok&&(await a.json()).models){ location.replace("/"); return; } }catch(_){}
  }
  setTimeout(poll,1000);
}
poll();
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/setup/status"):
            body, ctype = json.dumps(state).encode(), "application/json"
        elif self.path.split("?")[0] in ("/", "/index.html"):
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        else:   # e.g. /api/status: the app isn't up yet
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ------------------------------------------------------------------ setup steps

def run(cmd, env=None):
    log("$ " + " ".join(str(c) for c in cmd[:4]) + (" …" if len(cmd) > 4 else ""))
    p = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         env={**os.environ, **(env or {})}, text=True, bufsize=1)
    for line in p.stdout:
        line = line.rstrip()
        if line:
            print(line, flush=True)
            if line.startswith("PROGRESS "):
                done, total = map(int, line.split()[1:3])
                state["progress"] = done / total if total else None
                state["detail"] = f"{done / 1e9:.1f} of {total / 1e9:.1f} GB"
            else:
                state["log"] = (state["log"] + [line[-160:]])[-12:]
    if p.wait() != 0:
        raise RuntimeError(f"{Path(str(cmd[0])).name} failed (exit {p.returncode}). "
                           f"See {SUPPORT / 'murmur.log'}")


def setup():
    marker = VENV / ".murmur-lock"
    if not (PY.exists() and marker.exists() and marker.read_text() == lock_hash()):
        state.update(step="Installing Python packages", detail="This takes a few minutes the first time.",
                     progress=None)
        if not PY.exists():
            run([UV, "venv", "--python", "3.11", VENV])
        run([UV, "pip", "install", "--python", PY, "-r", LOCK])
        marker.write_text(lock_hash())


def main():
    SUPPORT.mkdir(parents=True, exist_ok=True)
    first_run = not (VENV / ".murmur-lock").exists()
    srv = None
    if first_run:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        webbrowser.open(URL)
    try:
        setup()
    except Exception as e:
        log(f"ERROR: {e}")
        state["error"] = str(e)
        if srv is None:     # already set up before: show the error page now
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            webbrowser.open(URL)
        while True:
            time.sleep(3600)

    state.update(step="Starting Murmur…", detail="", progress=1.0)
    log("starting server")
    if srv is not None:
        time.sleep(1.5)                 # let the page see the final state
        srv.shutdown()
        srv.server_close()
    # Models load strictly from the local cache, never checking Hugging Face for updates, so
    # Murmur works offline. (In-app model downloads run in a subprocess with network enabled.)
    env = {**os.environ, "MURMUR_DATA": str(SUPPORT / "data"), "MURMUR_PORT": str(PORT),
           "PYTHONUNBUFFERED": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
           "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"}
    if srv is None:
        # open the browser once the app answers (from a helper, since we exec below)
        subprocess.Popen(["/bin/sh", "-c", f"for i in $(seq 1 60); do curl -s -m 1 {URL}api/status "
                          f">/dev/null && open {URL} && exit; sleep 1; done"])
    os.execve(str(PY), [str(PY), str(RES / "server" / "app.py")], env)


if __name__ == "__main__":
    main()
