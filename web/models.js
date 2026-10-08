// Welcome screen (choose + download models) and the Models dialog (switch / add / remove).
let M = null;                       // /api/models response
const obChoice = { asr: null, llm: null };
let obTimer = null, mdTimer = null;

const gb = x => x < 1 ? `${Math.round(x * 1000)} MB` : `${x.toFixed(1)} GB`;
const dots = (n, max) => "●".repeat(n) + "○".repeat(max - n);
const byKind = k => M.models.filter(m => m.kind === k);
const modelOf = key => M.models.find(m => m.key === key);
const sysText = () => {
  const s = M.system;
  return `This Mac: ${s.chip || "Apple silicon"} · ${s.ram_gb} GB memory · ${Math.round(s.free_gb)} GB free disk space`;
};
const tooBig = m => m.ram && M.system.ram_gb && M.system.ram_gb < m.ram;

async function loadModels() { M = await api("/api/models"); return M; }

function needsSetup() {
  if (!M.onboarded) return true;
  const sel = M.selected;
  return !modelOf("diar").installed || !sel.asr || !modelOf(sel.asr).installed || !sel.llm || !modelOf(sel.llm).installed;
}

// ------------------------------------------------------------------ welcome screen
function optCard(m, group) {
  const el = document.createElement("button");
  el.type = "button";
  el.className = "opt" + (obChoice[group] === m.key ? " sel" : "") + (tooBig(m) ? " warn" : "");
  const rec = M.recommended[group] === m.key;
  const badge = rec ? '<span class="badge">Recommended</span>' : m.installed ? '<span class="badge ok">Installed</span>' : "";
  el.innerHTML = `${badge}<b></b><span class="d"></span>
    <span class="meta2">${m.installed ? "Installed ✓" : gb(m.size)}${m.ram ? ` · for ${m.ram}+ GB Macs` : ""}</span>
    ${m.kind === "llm" ? `<span class="meta2">Speed <span class="dots">${dots(m.speed, 3)}</span> · Quality <span class="dots">${dots(m.quality, 4)}</span></span>` : ""}
    <span class="meta2"></span>`;
  el.querySelector("b").textContent = m.name;
  el.querySelector(".d").textContent = m.desc;
  el.querySelector(".meta2:last-child").textContent = m.tag;
  el.title = tooBig(m) ? `Needs about ${m.ram} GB of memory — this Mac has ${M.system.ram_gb} GB, so it may be slow or not fit.` : m.tag;
  el.onclick = () => { obChoice[group] = m.key; renderOnboard(); };
  return el;
}

function renderOnboard() {
  $("#obSys").textContent = sysText();
  const a = $("#obAsr"), l = $("#obLlm");
  a.innerHTML = ""; l.innerHTML = "";
  byKind("asr").forEach(m => a.append(optCard(m, "asr")));
  byKind("llm").forEach(m => l.append(optCard(m, "llm")));
  const need = ["diar", obChoice.asr, obChoice.llm].map(modelOf).filter(m => m && !m.installed);
  const total = need.reduce((s, m) => s + m.size, 0);
  const go = $("#obGo");
  go.disabled = false;
  go.textContent = total ? `Download ${gb(total)} and get started` : "Get started";
  $("#obNote").textContent = total
    ? `Takes a few minutes on a fast connection. After that Murmur works without internet.`
    : `Everything you picked is already on this Mac.`;
}

function progressRows(box, keys) {
  box.innerHTML = "";
  let allDone = true, failed = false;
  for (const key of keys) {
    const m = modelOf(key);
    if (!m) continue;
    const d = m.download;
    if (m.installed && (!d || d.status === "done")) {
      box.insertAdjacentHTML("beforeend", `<div class="prow">✓ ${m.name} <span class="hint">${m.tag}</span></div>`);
      continue;
    }
    allDone = false;
    const row = document.createElement("div");
    row.className = "prow";
    if (d && d.status === "error") {
      failed = true;
      row.innerHTML = `<b></b> — <span class="err"></span> <button type="button" class="ghost small">Retry</button>`;
      row.querySelector(".err").textContent = d.error;
      row.querySelector("button").onclick = () => api(`/api/models/${key}/download`, { method: "POST" }).catch(e => toast(e.message));
    } else {
      const pct = d && d.total ? Math.min(100, d.done / d.total * 100) : 0;
      const label = !d || d.status === "queued" ? "waiting…" : `${gb(d.done / 1e9)} of ${gb(d.total / 1e9)}`;
      row.innerHTML = `<b></b> <span class="hint">${label}</span><div class="pbar"><i style="width:${pct.toFixed(1)}%"></i></div>`;
    }
    row.querySelector("b").textContent = m.name + (m.kind === "llm" ? " notes AI" : m.kind === "asr" ? " transcription" : "");
    box.append(row);
  }
  return { allDone, failed };
}

async function startOnboard() {
  const go = $("#obGo");
  go.disabled = true; go.textContent = "Starting…";
  try {
    M = await api("/api/onboarding", { method: "POST", body: obChoice });
  } catch (e) { toast(e.message); renderOnboard(); return; }
  followOnboard();
}

function followOnboard() {
  const keys = ["diar", obChoice.asr, obChoice.llm];
  document.querySelectorAll("#obAsr .opt, #obLlm .opt").forEach(b => b.disabled = true);
  clearInterval(obTimer);
  const tick = async () => {
    try { await loadModels(); } catch { return; }
    const { allDone, failed } = progressRows($("#obProgress"), keys);
    const go = $("#obGo");
    if (allDone) {
      clearInterval(obTimer);
      go.disabled = false; go.textContent = "Open Murmur";
      go.onclick = () => { $("#onboard").hidden = true; go.onclick = startOnboard; pollStatus(); };
      $("#obNote").textContent = "All set. Models load into memory in the background (a few seconds).";
    } else {
      go.disabled = true;
      go.textContent = failed ? "A download failed — retry above" : "Downloading…";
      $("#obNote").textContent = "You can keep this page open; downloads continue in the background.";
    }
  };
  tick();
  obTimer = setInterval(tick, 1000);
}

async function showOnboardingIfNeeded() {
  await loadModels();
  if (!needsSetup()) return;
  obChoice.asr = M.selected.asr || M.recommended.asr;
  obChoice.llm = M.selected.llm || M.recommended.llm;
  $("#onboard").hidden = false;
  renderOnboard();
  $("#obGo").onclick = startOnboard;
  if (M.models.some(m => m.download && ["queued", "downloading"].includes(m.download.status))) followOnboard();
}

// ------------------------------------------------------------------ Models dialog
function modelRow(m) {
  const row = document.createElement("div");
  row.className = "mrow";
  row.innerHTML = `<div class="info"><div><b></b> <span class="hint"></span></div><div class="d"></div></div><div class="act"></div>`;
  row.querySelector("b").textContent = m.name;
  row.querySelector(".hint").textContent = `${m.tag} · ${gb(m.size)}`;
  row.querySelector(".d").textContent = m.desc + (tooBig(m) ? ` — needs ~${m.ram} GB memory (this Mac: ${M.system.ram_gb} GB)` : "");
  const act = row.querySelector(".act");
  const d = m.download;
  const btn = (label, cls, fn) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = cls; b.textContent = label;
    b.onclick = async () => { b.disabled = true; try { await fn(); } catch (e) { toast(e.message); } await refreshModelsDlg(); };
    act.append(b);
  };
  if (d && ["queued", "downloading"].includes(d.status)) {
    const pct = d.total ? Math.min(100, d.done / d.total * 100) : 0;
    act.innerHTML = `<span class="hint">${d.status === "queued" ? "waiting…" : pct.toFixed(0) + "%"}</span><div class="pbar"><i style="width:${pct}%"></i></div>`;
  } else if (m.installed && m.selected) {
    act.innerHTML = `<span class="inuse">${m.kind === "diar" ? "Required · installed" : "In use"}</span>`;
  } else if (m.installed) {
    btn("Use", "primary small", async () => {
      await api(`/api/models/${m.key}/use`, { method: "POST" });
      toast(`Switched to ${m.name} — loading it now`);
    });
    btn("Remove", "ghost small danger", async () => {
      if (!confirm(`Remove ${m.name} (${m.tag})? It frees ${gb(m.size)}. You can download it again later.`)) return;
      await api(`/api/models/${m.key}`, { method: "DELETE" });
    });
  } else {
    if (d && d.status === "error") act.insertAdjacentHTML("beforeend", `<span class="hint" style="color:var(--danger)">failed</span>`);
    btn(`Download ${gb(m.size)}`, "small", () => api(`/api/models/${m.key}/download`, { method: "POST" }));
  }
  return row;
}

// Only rows whose content changed are rebuilt, so a periodic refresh never swallows a click
// on a button that is being pressed at that moment.
const rowSig = m => JSON.stringify([m.installed, m.selected, m.download && [m.download.status, Math.floor((m.download.done / (m.download.total || 1)) * 100)], M.system.ram_gb]);
async function refreshModelsDlg() {
  try { await loadModels(); } catch { return; }
  $("#mdSys").textContent = sysText();
  for (const [kind, id] of [["asr", "#mdAsr"], ["llm", "#mdLlm"], ["diar", "#mdDiar"]]) {
    const box = $(id);
    const want = byKind(kind);
    const rows = [...box.children];
    want.forEach((m, i) => {
      const sig = rowSig(m), cur = rows[i];
      if (cur && cur.dataset.key === m.key && cur.dataset.sig === sig) return;
      const row = modelRow(m);
      row.dataset.key = m.key; row.dataset.sig = sig;
      cur ? cur.replaceWith(row) : box.append(row);
    });
    for (let i = want.length; i < rows.length; i++) rows[i].remove();
  }
}

$("#modelsBtn").onclick = async () => {
  await refreshModelsDlg();
  $("#modelsDlg").showModal();
  clearInterval(mdTimer);
  mdTimer = setInterval(() => { if ($("#modelsDlg").open) refreshModelsDlg(); else clearInterval(mdTimer); }, 1500);
};

showOnboardingIfNeeded().catch(e => console.error(e));
