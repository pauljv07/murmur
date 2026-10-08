const $ = s => document.querySelector(s);
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
};
const toast = msg => {
  const t = document.createElement("div"); t.className = "toast"; t.textContent = msg;
  document.body.append(t); setTimeout(() => t.remove(), 2600);
};
const fmtTime = s => { s = Math.floor(s); return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };
const COLORS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s0)"];
const colorOf = spk => String(spk) === "me" ? "var(--accent)" : COLORS[+spk % 4];

let meetings = [], cur = null, tab = "notes", rec = null, busy = false;

// --------------------------------------------------------------- sidebar
async function loadList() {
  meetings = await api("/api/meetings");
  renderList();
}
function renderList() {
  const q = $("#search").value.toLowerCase();
  const groups = new Map();
  const today = new Date().toDateString(), yest = new Date(Date.now() - 864e5).toDateString();
  for (const m of meetings) {
    if (q && !(m.title || "").toLowerCase().includes(q)) continue;
    const d = new Date(m.created * 1000).toDateString();
    const label = d === today ? "Today" : d === yest ? "Yesterday"
      : new Date(m.created * 1000).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(m);
  }
  const nav = $("#list"); nav.innerHTML = "";
  for (const [label, items] of groups) {
    const h = document.createElement("h4"); h.textContent = label; nav.append(h);
    for (const m of items) {
      const a = document.createElement("a");
      a.className = cur && cur.id === m.id ? "active" : "";
      const time = new Date(m.created * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      a.innerHTML = `<div class="t"></div><div class="s">${time}${m.duration ? " · " + Math.max(1, Math.round(m.duration / 60)) + " min" : ""}</div>`;
      a.querySelector(".t").textContent = m.title || "Untitled meeting";
      a.onclick = () => open(m.id);
      nav.append(a);
    }
  }
}

// --------------------------------------------------------------- document
async function open(id) {
  if (rec) { toast("Stop recording first"); return; }
  try { cur = await api(`/api/meetings/${id}`); }
  catch { toast("That note no longer exists"); await loadList(); return; }
  $("#empty").hidden = true; $("#doc").hidden = false; $("#bar").hidden = false;
  $("#chatPanel").hidden = true; $("#chatLog").innerHTML = "";
  $("#title").value = cur.title || "";
  $("#notes").value = cur.notes || "";
  $("#vocab").value = cur.vocabulary || "";
  $("#template").value = cur.template || "general";
  $("#date").textContent = new Date(cur.created * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  setTab(cur.enhanced ? "enhanced" : "notes");
  renderTranscript(); renderSpeakers(); renderList();
  history.replaceState(null, "", `#${id}`);
}

function setTab(t) {
  tab = t;
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === t));
  $("#notes").hidden = t !== "notes";
  $("#enhancedEdit").hidden = true;
  $("#enhancedView").hidden = t !== "enhanced";
  $("#editEnhanced").hidden = t !== "enhanced" || !cur.enhanced;
  $("#editEnhanced").textContent = "Edit";
  if (t === "enhanced") renderEnhanced();
}

// Lines in the enhanced notes that came from the user's own notes are shown in full ink,
// AI additions in grey.
function isMine(line) {
  const words = s => new Set(s.toLowerCase().match(/[a-z0-9']{3,}/g) || []);
  const w = words(line); if (!w.size) return false;
  for (const n of (cur.notes || "").split("\n")) {
    const nw = words(n); if (!nw.size) continue;
    let hit = 0; nw.forEach(x => w.has(x) && hit++);
    if (hit / nw.size >= 0.5 && hit >= Math.min(2, nw.size)) return true;
  }
  return false;
}
function renderEnhanced(streaming) {
  const v = $("#enhancedView");
  if (!cur.enhanced && !streaming) {
    v.innerHTML = `<p class="ai">No enhanced notes yet. Record a meeting, then press <b>✦ Enhance</b>.</p>`;
    return;
  }
  v.innerHTML = renderMarkdown(cur.enhanced || "", (cur.notes || "").trim() ? isMine : null);
  v.classList.toggle("cursor", !!streaming);
}

let saveTimer;
function saveSoon(fields) {
  Object.assign(cur, fields);
  clearTimeout(saveTimer);
  const id = cur.id, snapshot = { ...fields };
  saveTimer = setTimeout(async () => {
    await api(`/api/meetings/${id}`, { method: "PATCH", body: snapshot });
    const m = meetings.find(x => x.id === id); if (m && "title" in snapshot) { m.title = snapshot.title; renderList(); }
  }, 400);
}

// --------------------------------------------------------------- speakers + transcript
const speakerName = s => (cur.speakers || {})[s] || (s === "me" ? "Me" : `Speaker ${+s + 1}`);
function renderSpeakers() {
  const ids = [...new Set((cur.transcript || []).map(s => String(s.speaker)))]
    .sort((a, b) => a === "me" ? -1 : b === "me" ? 1 : a - b);
  const box = $("#speakers"); box.innerHTML = "";
  for (const id of ids) {
    const c = document.createElement("span"); c.className = "chip"; c.title = "Click to rename";
    c.innerHTML = `<i style="background:${colorOf(id)}"></i>`;
    c.append(speakerName(id));
    c.onclick = async () => {
      const n = prompt("Name for this speaker", speakerName(id));
      if (n === null) return;
      cur.speakers = { ...(cur.speakers || {}), [id]: n.trim() };
      await api(`/api/meetings/${cur.id}`, { method: "PATCH", body: { speakers: cur.speakers } });
      renderSpeakers(); renderTranscript();
    };
    box.append(c);
  }
}
function segEl(s) {
  const d = document.createElement("div"); d.className = "seg";
  d.innerHTML = `<div class="who" style="color:${colorOf(s.speaker)}"><span></span><time>${fmtTime(s.start)}</time></div><div class="txt"></div>`;
  d.querySelector(".who span").textContent = speakerName(String(s.speaker));
  d.querySelector(".txt").textContent = s.text;
  return d;
}
function renderTranscript() {
  const b = $("#txBody"); b.innerHTML = "";
  for (const s of cur.transcript || []) b.append(segEl(s));
  if (!(cur.transcript || []).length) b.innerHTML = `<p class="status">Nothing transcribed yet.</p>`;
  b.scrollTop = b.scrollHeight;
}
function setTranscript(tr) {
  const b = $("#txBody");
  const nearBottom = b.scrollHeight - b.scrollTop - b.clientHeight < 80;
  cur.transcript = tr;
  b.innerHTML = "";
  for (const s of tr) b.append(segEl(s));
  if (nearBottom) b.scrollTop = b.scrollHeight;
  renderSpeakers();
}

// --------------------------------------------------------------- recording
async function newNote() {
  const m = await api("/api/meetings", { method: "POST", body: {} });
  await loadList(); await open(m.id);
  return cur;
}

// Make sure the note we're about to record into exists on the server (it may have been
// deleted elsewhere, or no note is open yet); fall back to a fresh note.
async function ensureNote() {
  if (cur) {
    try { cur = await api(`/api/meetings/${cur.id}`); return cur; }
    catch { toast("That note no longer exists — recording into a new note"); }
  }
  return newNote();
}

async function startRecording() {
  const m = await ensureNote();
  const source = $("#source").value;
  const mic = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
  const streams = [mic];
  let sysStream = null;
  if (source === "browser") {
    try {
      const disp = await navigator.mediaDevices.getDisplayMedia({
        video: true, systemAudio: "include",
        // keep the remote audio untouched: processing it only hurts recognition
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
      disp.getVideoTracks().forEach(t => t.stop());
      if (disp.getAudioTracks().length) { sysStream = disp; streams.push(disp); }
      else toast("No audio was shared — recording mic only");
    } catch { toast("Nothing shared — recording mic only"); }
  }
  const stereo = !!sysStream;
  const ctx = new AudioContext({ sampleRate: 16000 });
  await ctx.audioWorklet.addModule("/static/recorder-worklet.js");
  const node = new AudioWorkletNode(ctx, "recorder", { numberOfInputs: 2, processorOptions: { stereo } });
  const analyser = ctx.createAnalyser(); analyser.fftSize = 512;
  ctx.createMediaStreamSource(mic).connect(node, 0, 0);
  ctx.createMediaStreamSource(mic).connect(analyser);
  if (sysStream) {
    ctx.createMediaStreamSource(sysStream).connect(node, 0, 1);
    ctx.createMediaStreamSource(sysStream).connect(analyser);
  }
  const cleanup = () => { streams.forEach(s => s.getTracks().forEach(t => t.stop())); ctx.close(); };
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/record/${m.id}`);
  ws.binaryType = "arraybuffer";
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (!cur || cur.id !== m.id) return;          // user navigated away mid-message
    if (msg.type === "transcript") setTranscript(msg.transcript);
    else if (msg.type === "partial") $("#partial").textContent = msg.text;
    else if (msg.type === "status") $("#partial").textContent = msg.text;
    else if (msg.type === "error") toast(msg.text);
  };
  // attach before awaiting open so an immediate server-side close is never missed
  ws.onclose = () => { if (rec && rec.ws === ws) stopRecording(true); };
  try {
    await new Promise((res, rej) => {
      ws.onopen = res;
      ws.onerror = () => rej(new Error("could not connect to the local server"));
    });
  } catch (e) { cleanup(); throw e; }
  ws.send(JSON.stringify({ type: "start", system: stereo ? "browser" : source === "native" ? "native" : "none" }));
  node.port.onmessage = e => { if (ws.readyState === 1) ws.send(e.data.buffer); };

  rec = { ws, ctx, streams, analyser, started: Date.now(), meeting: m };
  if (ws.readyState !== 1) { stopRecording(true); throw new Error("server closed the connection"); }
  $("#recBtn").classList.add("on");
  $("#transcript").hidden = false;
  if (!(m.transcript || []).length) $("#txBody").innerHTML = "";
  tick();
}
function tick() {
  if (!rec) return;
  $("#recLabel").textContent = fmtTime((Date.now() - rec.started) / 1000);
  const data = new Uint8Array(rec.analyser.fftSize); rec.analyser.getByteTimeDomainData(data);
  const c = $("#meter").getContext("2d"), W = 44, H = 22; c.clearRect(0, 0, W, H);
  c.fillStyle = getComputedStyle(document.body).getPropertyValue("--rec");
  for (let i = 0; i < 7; i++) {
    let peak = 0;
    for (let j = i * 64; j < (i + 1) * 64; j++) peak = Math.max(peak, Math.abs(data[j] - 128) / 128);
    const h = Math.max(2, Math.min(H, peak * H * 2.5));
    c.fillRect(i * 6 + 1, (H - h) / 2, 4, h);
  }
  requestAnimationFrame(tick);
}
async function stopRecording(remote) {
  const r = rec; rec = null; if (!r) return;
  $("#recBtn").classList.remove("on"); $("#recLabel").textContent = "Record";
  r.streams.forEach(s => s.getTracks().forEach(t => t.stop()));
  await r.ctx.close();
  if (!remote && r.ws.readyState === 1) {
    $("#partial").textContent = "Finishing transcription…";
    r.ws.send(JSON.stringify({ type: "stop" }));
    await new Promise(res => { r.ws.onclose = res; setTimeout(res, 60000); });
  }
  $("#partial").textContent = "";
  const id = r.meeting.id;
  let m;
  try { m = await api(`/api/meetings/${id}`); }
  catch { toast("Recording stopped — this note was removed"); await loadList(); return; }
  if (cur && cur.id === id) { cur = m; renderTranscript(); renderSpeakers(); }
  await loadList();
  if (!m.title && m.transcript.length) {
    try {
      const { title } = await api(`/api/meetings/${id}/title`, { method: "POST" });
      if (title && cur && cur.id === id) { $("#title").value = title; cur.title = title; }
      await loadList();
    } catch { /* title is a nicety */ }
  }
}

// --------------------------------------------------------------- streaming LLM calls
async function streamSSE(path, body, onText) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  if (!r.ok) throw new Error(await r.text());
  const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
  for (;;) {
    const { value, done } = await rd.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const ev = buf.slice(0, i); buf = buf.slice(i + 2);
      if (!ev.startsWith("data: ")) continue;
      const d = JSON.parse(ev.slice(6));
      if (d.error) throw new Error(d.error);
      if (d.text) onText(d.text);
    }
  }
}

async function enhance() {
  if (busy) return;
  if (rec) { toast("Stop recording first"); return; }
  if (!cur.transcript.length && !cur.notes.trim()) { toast("Nothing to enhance yet"); return; }
  busy = true; $("#enhanceBtn").disabled = true; $("#enhanceBtn").textContent = "Writing…";
  await api(`/api/meetings/${cur.id}`, { method: "PATCH", body: { notes: $("#notes").value, template: $("#template").value } });
  cur.notes = $("#notes").value; cur.enhanced = ""; setTab("enhanced"); renderEnhanced(true);
  try {
    await streamSSE(`/api/meetings/${cur.id}/enhance`, {}, t => { cur.enhanced += t; renderEnhanced(true); });
  } catch (e) { toast("Enhance failed: " + e.message); }
  renderEnhanced(); setTab("enhanced");
  busy = false; $("#enhanceBtn").disabled = false; $("#enhanceBtn").textContent = "✦ Enhance";
}

async function ask(q) {
  if (busy) { toast("Still working…"); return; }
  busy = true;
  $("#chatPanel").hidden = false;
  const qEl = document.createElement("div"); qEl.className = "q"; qEl.textContent = q;
  const aEl = document.createElement("div"); aEl.className = "a md cursor";
  $("#chatLog").append(qEl, aEl);
  let ans = "";
  try {
    await streamSSE(`/api/meetings/${cur.id}/chat`, { question: q }, t => {
      ans += t; aEl.innerHTML = renderMarkdown(ans);
      $("#chatPanel").scrollTop = $("#chatPanel").scrollHeight;
    });
  } catch (e) { aEl.textContent = "Error: " + e.message; }
  aEl.classList.remove("cursor"); busy = false;
}

// --------------------------------------------------------------- wiring
$("#newBtn").onclick = async () => {
  if (rec) { toast("Stop recording first"); return; }
  await newNote(); $("#title").focus();
};
$("#search").oninput = renderList;
$("#title").oninput = e => saveSoon({ title: e.target.value });
$("#notes").oninput = e => saveSoon({ notes: e.target.value });
$("#template").onchange = e => saveSoon({ template: e.target.value });
$("#vocab").oninput = e => saveSoon({ vocabulary: e.target.value });
$("#source").onchange = e => { try { localStorage.setItem("murmur.source", e.target.value); } catch {} };
$("#vocabBtn").onclick = async () => {
  $("#vocabText").value = (await api("/api/vocabulary")).text;
  $("#vocabDlg").showModal();
};
$("#vocabSave").onclick = async () => {
  await api("/api/vocabulary", { method: "PUT", body: { text: $("#vocabText").value } });
  toast("Vocabulary saved");
};
document.querySelectorAll(".tab").forEach(b => b.onclick = () => setTab(b.dataset.tab));
$("#editEnhanced").onclick = () => {
  const ed = $("#enhancedEdit");
  if (ed.hidden) {
    ed.value = cur.enhanced; ed.hidden = false; $("#enhancedView").hidden = true; $("#editEnhanced").textContent = "Done";
  } else {
    saveSoon({ enhanced: ed.value }); ed.hidden = true; $("#enhancedView").hidden = false;
    $("#editEnhanced").textContent = "Edit"; renderEnhanced();
  }
};
$("#copyBtn").onclick = async () => {
  const text = tab === "enhanced" && cur.enhanced ? cur.enhanced : $("#notes").value;
  await navigator.clipboard.writeText(`# ${cur.title || "Untitled meeting"}\n\n${text}`);
  toast("Copied as Markdown");
};
$("#deleteBtn").onclick = async () => {
  if (rec || !confirm("Delete this meeting, its notes and its audio?")) return;
  await api(`/api/meetings/${cur.id}`, { method: "DELETE" });
  cur = null; $("#doc").hidden = true; $("#bar").hidden = true; $("#transcript").hidden = true;
  $("#empty").hidden = false; history.replaceState(null, "", "#"); loadList();
};
$("#recBtn").onclick = async () => {
  if (rec) return stopRecording(false);
  $("#recBtn").disabled = true;
  try { await startRecording(); } catch (e) { console.error(e); toast("Could not start: " + e.message); }
  $("#recBtn").disabled = false;
};
$("#txBtn").onclick = () => { $("#transcript").hidden = !$("#transcript").hidden; };
$("#txClose").onclick = () => { $("#transcript").hidden = true; };
$("#enhanceBtn").onclick = enhance;
$("#askForm").onsubmit = e => {
  e.preventDefault(); const q = $("#ask").value.trim(); if (!q) return;
  $("#ask").value = ""; ask(q);
};
$("#redoBtn").onclick = async () => {
  if (rec || busy) return;
  busy = true; $("#redoBtn").disabled = true; $("#partial").textContent = "Re-processing full recording…";
  try {
    cur = await api(`/api/meetings/${cur.id}/reprocess`, { method: "POST" });
    renderTranscript(); renderSpeakers(); toast("Transcript refined");
  } catch (e) { toast("Refine failed: " + e.message); }
  $("#partial").textContent = ""; $("#redoBtn").disabled = false; busy = false;
};
document.addEventListener("keydown", e => {
  if (e.key === "Escape") $("#chatPanel").hidden = true;
});

async function pollStatus() {
  try {
    const s = await api("/api/status");
    const nat = $("#source option[value=native]");
    nat.disabled = !s.native_audio;
    if (!s.native_audio && $("#source").value === "native") $("#source").value = "browser";
    $("#modelStatus").innerHTML = Object.entries(s.models)
      .map(([k, v]) => `<div>${v === "ready" ? '<span class="ok">●</span>' : "○"} ${k}: ${v}</div>`).join("");
    if (Object.values(s.models).some(v => v !== "ready")) setTimeout(pollStatus, 3000);
  } catch { setTimeout(pollStatus, 3000); }
}

(async () => {
  const t = await api("/api/templates");
  $("#template").innerHTML = Object.entries(t).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  try { const src = localStorage.getItem("murmur.source"); if (src) $("#source").value = src; } catch {}
  await loadList();
  const id = location.hash.slice(1);
  if (id && meetings.some(m => m.id === id)) open(id);
  pollStatus();
})();
