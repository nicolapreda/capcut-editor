"use strict";

const BACKEND = "http://127.0.0.1:8765";
const WS_BACKEND = "ws://127.0.0.1:8765";

// LOCAL_MODE = true → nessun login né controllo abbonamento (uso privato).
// Metti false quando il backend cloud è pronto per la vendita.
const LOCAL_MODE = true;

const PACING_HINTS = {
  normal: "silenzi ≥ 0.45s tagliati · pad finale 0.22s",
  fast: "silenzi ≥ 0.25s tagliati · pad finale 0.18s",
  aggressive: "silenzi ≥ 0.12s tagliati · nessun pad finale",
};

const DEFAULTS = {
  input_path: "", name: "", script_path: "", intro_title: "",
  model: "large-v3", language: "it", pacing: "fast", fmt: "vertical",
  subtitle_max_words: 4, subtitle_max_duration: 1.4, subtitle_like_template: true,
  drop_fillers: true, aggressive_fillers: false,
  clear_template_texts: true, redistribute_sfx_enabled: true, stabilize_clips: false,
  ai_model: "sonnet", use_ai_review: true,
  ai_target_duration: null,
};

const state = {
  backendReady: false,
  aiReady: false,          // Claude verified with a real request — required to generate
  running: false,
  pacing: "fast",
  format: "vertical",
  defaultTemplate: "",
  wizardStep: 1,
  batch: false,
  batchCount: 0,
  authMode: "login",       // login | register
  authToken: null,
  cloudUrl: "http://127.0.0.1:8799",
  email: null,
};

// Offline grace: how long the app keeps working after the last confirmed
// active subscription check, when the cloud can't be reached.
const OFFLINE_GRACE_DAYS = 7;

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

window.addEventListener("DOMContentLoaded", async () => {
  wireStaticControls();
  wireWizard();
  wireAuth();
  $("pacing-hint").textContent = PACING_HINTS.fast;

  // Resolve the cloud licensing URL (Electron injects it; browser dev falls back)
  if (window.api && window.api.cloudUrl) {
    try { state.cloudUrl = await window.api.cloudUrl(); } catch (_) {}
  }

  // Electron tells us when the sidecar is up; we also poll as a fallback.
  if (window.api && window.api.onBackendReady) {
    window.api.onBackendReady(() => bootBackend());
  }
  bootBackend();          // sidecar readiness (pills + generate), runs regardless

  if (LOCAL_MODE) {
    showView("home");     // private use: skip the login/subscription gate
  } else {
    startAuthGate();      // decides the first visible screen
  }
});

let _booting = false;

async function bootBackend() {
  if (_booting || state.backendReady) return;
  _booting = true;
  for (let i = 0; i < 120; i++) {          // ~60s of retries
    try {
      const r = await fetch(`${BACKEND}/api/health`);
      if (r.ok) {
        state.backendReady = true;
        setPill("pill-backend", "ok", "backend");
        await loadSystem();
        await loadTemplates();
        await loadProjects();
        refreshGenerateEnabled();
        checkClaude();                      // real request, takes a few seconds
        _booting = false;
        return;
      }
    } catch (_) { /* backend not up yet */ }
    await new Promise((res) => setTimeout(res, 500));
  }
  setPill("pill-backend", "err", "backend");
  _booting = false;
}

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------

function showView(name) {
  for (const v of ["view-auth", "view-subscribe", "view-home", "view-editor"]) {
    $(v).hidden = v !== `view-${name}`;
  }
  // account controls only when past the gate (and not in local/private mode)
  const authed = (name === "home" || name === "editor") && !LOCAL_MODE;
  $("tb-email").hidden = !authed || !state.email;
  $("tb-account").hidden = !authed;
  $("tb-logout").hidden = !authed;
  if (state.email) $("tb-email").textContent = state.email;
  if (name === "home" && state.backendReady) loadProjects();
}

// ---------------------------------------------------------------------------
// Data loads
// ---------------------------------------------------------------------------

// Claude is mandatory: verify it with a real request and gate "Genera" on it.
async function checkClaude() {
  const box = $("ai-status");
  box.className = "ai-status";
  box.textContent = "Verifico il collegamento a Claude…";
  setPill("pill-ai", "warn", "AI…");
  try {
    const r = await (await fetch(`${BACKEND}/api/ai/test`, { method: "POST" })).json();
    state.aiReady = !!r.ok;
    if (r.ok) {
      const via = r.method === "claude-cli" ? "Claude Code CLI" : "API key Anthropic";
      box.classList.add("ok");
      box.textContent = `✓ Claude collegato (${via}) — pronto a montare.`;
      setPill("pill-ai", "ok", "AI");
    } else {
      box.classList.add("err");
      box.textContent = `✘ Claude non collegato: ${r.message} — senza Claude non si può generare.`;
      setPill("pill-ai", "err", "AI");
    }
  } catch (_) {
    state.aiReady = false;
    box.classList.add("err");
    box.textContent = "✘ Impossibile verificare Claude (backend non raggiungibile).";
    setPill("pill-ai", "err", "AI");
  }
  refreshGenerateEnabled();
}

async function loadSystem() {
  try {
    const sys = await (await fetch(`${BACKEND}/api/system`)).json();
    setPill("pill-ffmpeg", sys.ffmpeg ? "ok" : "err", "ffmpeg");
    const aiFound = sys.ai && (sys.ai.claude_cli || sys.ai.anthropic_key);
    if (!aiFound) setPill("pill-ai", "err", "AI");   // checkClaude() sets the final state
    const cached = (sys.whisper_models_cached || []).join(", ");
    $("model-hint").textContent = cached
      ? `Modelli già scaricati: ${cached}`
      : "Nessun modello in cache — il primo uso scaricherà ~3GB per large-v3.";
  } catch (e) { console.error("system", e); }
}

async function loadTemplates() {
  try {
    const data = await (await fetch(`${BACKEND}/api/templates`)).json();
    state.defaultTemplate = data.default || "";
    const sel = $("template");
    sel.innerHTML = "";
    const none = document.createElement("option");
    none.value = ""; none.textContent = "(nessun template)";
    sel.appendChild(none);
    for (const t of data.templates) {
      const o = document.createElement("option");
      o.value = t.name; o.textContent = t.name;
      if (data.default && t.name === data.default) o.selected = true;
      sel.appendChild(o);
    }
  } catch (e) { console.error("templates", e); }
}

async function loadProjects() {
  try {
    const data = await (await fetch(`${BACKEND}/api/projects`)).json();
    renderProjects(data.projects || []);
  } catch (e) { console.error("projects", e); }
}

function renderProjects(projects) {
  const grid = $("projects-grid");
  const empty = $("projects-empty");
  grid.innerHTML = "";
  if (!projects.length) {
    grid.hidden = true; empty.hidden = false; return;
  }
  grid.hidden = false; empty.hidden = true;

  for (const p of projects) {
    const card = document.createElement("div");
    card.className = "project-card" + (p.exists ? "" : " missing");
    const tmpl = p.settings && p.settings.template ? p.settings.template : "—";
    const date = new Date((p.created_at || 0) * 1000);
    const dateStr = date.toLocaleDateString("it-IT", { day: "2-digit", month: "short", year: "numeric" });

    card.innerHTML = `
      <div class="pc-name">${escapeHtml(p.name)}</div>
      <div class="pc-meta">
        <span class="pc-badge">${escapeHtml(tmpl)}</span>
        <span>${dateStr}</span>
        ${p.exists ? "" : '<span style="color:var(--err)">cartella rimossa</span>'}
      </div>
      <div class="pc-actions">
        <button class="pc-open" ${p.exists ? "" : "disabled"}>Apri</button>
        <button class="pc-dup">Duplica</button>
      </div>`;

    card.querySelector(".pc-open").onclick = () => {
      if (p.exists) window.api.openPath(p.result_path);
    };
    card.querySelector(".pc-dup").onclick = () => duplicateProject(p.id);
    grid.appendChild(card);
  }
}

// ---------------------------------------------------------------------------
// New / duplicate
// ---------------------------------------------------------------------------

function resetBatch() {
  state.batch = false;
  state.batchCount = 0;
  setSwitch("sw-batch", false);
  updateBatchUI();
}

function newProject() {
  applySettings(DEFAULTS);
  // keep the loaded default template selected
  if (state.defaultTemplate) $("template").value = state.defaultTemplate;
  resetBatch();
  $("editor-title").textContent = "Nuovo progetto";
  resetConsole();
  showView("editor");
  $("name").focus();
}

async function duplicateProject(id) {
  try {
    const p = await (await fetch(`${BACKEND}/api/projects/${id}`)).json();
    if (p.error) return;
    const s = p.settings || {};
    applySettings({ ...DEFAULTS, ...s });
    resetBatch();
    $("name").value = `${p.name} copia`;
    $("editor-title").textContent = `Duplica: ${p.name}`;
    resetConsole();
    showView("editor");
    $("name").focus();
    $("name").select();
  } catch (e) { console.error("duplicate", e); }
}

function applySettings(s) {
  $("input-path").value = s.input_path || "";
  $("name").value = s.name || "";
  $("template").value = s.template || "";
  $("script-path").value = s.script_path || "";
  $("intro-title").value = s.intro_title || "";
  $("model").value = s.model || "large-v3";
  $("language").value = s.language || "it";

  setSegmented("pacing", s.pacing || "fast", "pacing");
  $("pacing-hint").textContent = PACING_HINTS[s.pacing || "fast"] || "";
  setSegmented("format", s.fmt || "vertical", "format");

  $("sub-words").value = s.subtitle_max_words ?? 4;
  $("sub-words-val").textContent = String(s.subtitle_max_words ?? 4);
  $("sub-dur").value = s.subtitle_max_duration ?? 1.4;
  $("sub-dur-val").textContent = `${(s.subtitle_max_duration ?? 1.4).toFixed(1)} s`;

  setSwitch("sw-sub-template", s.subtitle_like_template ?? true);
  syncSubtitleMode();
  setSwitch("sw-fillers", s.drop_fillers);
  setSwitch("sw-aggr", s.aggressive_fillers);
  setSwitch("sw-clear", s.clear_template_texts);
  setSwitch("sw-sfx", s.redistribute_sfx_enabled);
  setSwitch("sw-stab", s.stabilize_clips);
  setSwitch("sw-ai-review", s.use_ai_review ?? true);

  $("ai-model").value = s.ai_model || "sonnet";
  $("ai-target").value = s.ai_target_duration == null ? "auto" : String(Math.round(s.ai_target_duration));
  refreshGenerateEnabled();
}

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------

function wireStaticControls() {
  $("btn-new").onclick = newProject;
  $("btn-new-empty").onclick = newProject;
  $("btn-back").onclick = () => showView("home");

  $("btn-folder").onclick = async () => {
    const p = await window.api.selectFolder();
    if (p) {
      $("input-path").value = p;
      if (state.batch) previewBatch();
      refreshGenerateEnabled();
    }
  };
  $("btn-video").onclick = async () => {
    const p = await window.api.selectVideo();
    if (p) { $("input-path").value = p; refreshGenerateEnabled(); }
  };
  $("btn-script").onclick = async () => {
    const p = await window.api.selectScript();
    if (p) $("script-path").value = p;
  };
  $("input-path").oninput = refreshGenerateEnabled;
  $("name").oninput = refreshGenerateEnabled;

  wireSegmented("pacing", (val) => {
    state.pacing = val;
    $("pacing-hint").textContent = PACING_HINTS[val] || "";
  });
  wireSegmented("format", (val) => { state.format = val; });

  document.querySelectorAll(".switch").forEach((sw) => {
    sw.onclick = () => sw.classList.toggle("on");
  });
  $("sw-sub-template").onclick = () => {
    $("sw-sub-template").classList.toggle("on");
    syncSubtitleMode();
  };
  // batch toggle needs extra behaviour on top of the plain on/off
  $("sw-batch").onclick = () => {
    $("sw-batch").classList.toggle("on");
    state.batch = isOn("sw-batch");
    updateBatchUI();
  };

  $("sub-words").oninput = (e) => { $("sub-words-val").textContent = e.target.value; };
  $("sub-dur").oninput = (e) => {
    $("sub-dur-val").textContent = `${parseFloat(e.target.value).toFixed(1)} s`;
  };

  $("btn-generate").onclick = onGenerate;
  $("btn-open").onclick = () => {
    const p = $("result-path").dataset.path;
    if (p) window.api.openPath(p);
  };
  $("btn-report").onclick = () => {
    const p = $("result-path").dataset.report;
    if (p) window.api.openPath(p);
  };
}

function wireSegmented(id, onChange) {
  const group = $(id);
  group.querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => {
      group.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      onChange(btn.dataset.val);
    };
  });
}

function setSegmented(id, val, stateKey) {
  const group = $(id);
  group.querySelectorAll("button").forEach((b) => {
    b.classList.toggle("active", b.dataset.val === val);
  });
  if (stateKey) state[stateKey === "pacing" ? "pacing" : "format"] = val;
}

function setPill(id, cls, label) {
  $(id).innerHTML = `<span class="dot ${cls}"></span> ${label}`;
}

function setSwitch(id, on) { $(id).classList.toggle("on", !!on); }

// The two sliders only count when the subtitles are sized by hand.
function syncSubtitleMode() {
  const likeTemplate = isOn("sw-sub-template");
  document.querySelectorAll(".sub-manual").forEach((row) => {
    row.classList.toggle("disabled", likeTemplate);
    row.querySelector("input").disabled = likeTemplate;
  });
  $("sub-hint").textContent = likeTemplate
    ? "Ogni sottotitolo è una copia esatta di uno del template: stesso modello di testo, animazione, effetto, font e posizione. Se il template non ha sottotitoli valgono i due cursori qui sotto."
    : "Lo stile resta quello del template; la lunghezza dei blocchi la decidi tu con i due cursori.";
}
function isOn(id) { return $(id).classList.contains("on"); }

function refreshGenerateEnabled() {
  const hasSource = !!$("input-path").value.trim();
  const hasName = state.batch ? true : !!$("name").value.trim();  // prefix optional in batch
  const batchOk = !state.batch || state.batchCount > 0;
  const ready = state.backendReady && state.aiReady && !state.running
    && hasSource && hasName && batchOk;
  const btn = $("btn-generate");
  btn.disabled = !ready;
  btn.title = state.aiReady ? "" : "Serve Claude collegato: l'AI fa il montaggio.";
}

function updateBatchUI() {
  const b = state.batch;
  $("src-label").textContent = b
    ? "Cartella madre (una sottocartella = un progetto)"
    : "Cartella video o singolo file";
  $("name-label").textContent = b ? "Prefisso nome (opzionale)" : "Nome progetto";
  $("name").placeholder = b ? "es. CLIENTE X (facoltativo)" : "es. Reel 23 maggio";
  $("input-path").placeholder = b
    ? "Nessuna cartella madre selezionata" : "Nessuna sorgente selezionata";
  $("btn-video").hidden = b;                    // single-file picker off in batch
  $("batch-preview").hidden = true;
  $("batch-preview").textContent = "";
  state.batchCount = 0;
  if (b && $("input-path").value.trim()) previewBatch();
  updateGenerateLabel();
  refreshGenerateEnabled();
}

function updateGenerateLabel() {
  if (state.running) return;
  const btn = $("btn-generate");
  if (state.batch) {
    btn.textContent = state.batchCount ? `GENERA ${state.batchCount} PROGETTI` : "GENERA TUTTI";
  } else {
    btn.textContent = "GENERA PROGETTO";
  }
}

async function previewBatch() {
  const path = $("input-path").value.trim();
  if (!path) return;
  try {
    const data = await (await fetch(
      `${BACKEND}/api/subfolders?path=${encodeURIComponent(path)}`)).json();
    const folders = data.folders || [];
    state.batchCount = folders.length;
    const el = $("batch-preview");
    el.hidden = false;
    el.textContent = folders.length
      ? `Troverò ${folders.length} progetti: ${folders.map((f) => f.name).join(", ")}`
      : "Nessuna sottocartella con video trovata qui.";
  } catch (_) {
    state.batchCount = 0;
  }
  updateGenerateLabel();
  refreshGenerateEnabled();
}

// ---------------------------------------------------------------------------
// Generate
// ---------------------------------------------------------------------------

function buildRequest() {
  const aiTarget = $("ai-target").value;
  return {
    input_path: $("input-path").value.trim(),
    name: $("name").value.trim(),
    template: $("template").value || null,
    script_path: $("script-path").value.trim() || null,
    intro_title: $("intro-title").value.trim() || null,
    model: $("model").value,
    language: $("language").value,
    pacing: state.pacing,
    fmt: state.format,
    subtitle_max_words: parseInt($("sub-words").value, 10),
    subtitle_max_duration: parseFloat($("sub-dur").value),
    subtitle_like_template: isOn("sw-sub-template"),
    drop_fillers: isOn("sw-fillers"),
    aggressive_fillers: isOn("sw-aggr"),
    clear_template_texts: isOn("sw-clear"),
    redistribute_sfx_enabled: isOn("sw-sfx"),
    stabilize_clips: isOn("sw-stab"),
    ai_model: $("ai-model").value,
    use_ai_review: isOn("sw-ai-review"),
    ai_target_duration: aiTarget === "auto" ? null : parseFloat(aiTarget),
  };
}

async function onGenerate() {
  state.running = true;
  refreshGenerateEnabled();
  clearConsole();
  $("result-box").classList.remove("show");
  $("btn-generate").innerHTML = '<span class="spinner"></span>Lavoro in corso…';
  setStatus("in esecuzione…");

  // choose single vs batch endpoint
  let endpoint = "/api/generate";
  let body = buildRequest();
  if (state.batch) {
    endpoint = "/api/batch";
    const parent = body.input_path;
    const prefix = body.name;
    delete body.input_path;
    delete body.name;
    body.parent_folder = parent;
    body.name_prefix = prefix;
  }

  let jobId;
  try {
    const r = await fetch(`${BACKEND}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    jobId = (await r.json()).job_id;
  } catch (e) {
    appendLine(`ERRORE: il motore dell'app non risponde (${e}). Chiudi tutte le finestre `
      + "di CapWiz e riavvia l'app: di solito ne erano aperte due.", "err");
    finishRun();
    return;
  }

  const ws = new WebSocket(`${WS_BACKEND}/api/ws/jobs/${jobId}`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "log") {
      appendLine(msg.line, classify(msg.line));
    } else if (msg.type === "status") {
      if (msg.status === "done") {
        setStatus("completato");
        if (msg.results && msg.results.length) showBatchResult(msg.results);
        else showResult(msg.result_path, msg.report_path);
        loadProjects();               // refresh home in the background
      } else {
        setStatus("errore");
        appendLine(msg.error || "errore sconosciuto", "err");
      }
      ws.close();
      finishRun();
    } else if (msg.type === "error") {
      appendLine(msg.message, "err");
      finishRun();
    }
  };
  ws.onerror = () => {
    appendLine("ERRORE: connessione WebSocket interrotta", "err");
    finishRun();
  };
}

function finishRun() {
  state.running = false;
  updateGenerateLabel();
  refreshGenerateEnabled();
}

function classify(line) {
  const t = line.trim();
  if (t.startsWith("━━") || t.startsWith("▶▶")) return "section";
  if (t.startsWith("✔")) return "keep";
  if (t.startsWith("✘ INTERROTTO") || t.startsWith("ERRORE")) return "err";
  if (t.startsWith("✘")) return "drop";
  if (t.startsWith("perché:") || t.startsWith("«")) return "why";
  if (t.startsWith("✂")) return "cut";
  if (t.startsWith("✓")) return "ok";
  if (t.includes("⚠") || t.startsWith("☐")) return "warn";
  if (/^(👁|🎞|🔊|🔇|♪|✚|✎|Titolo iniziale|Argomento|Come l|Soglia|Regia|Formato|Tono|Struttura|Indicazioni|Consegne|Ritmo dal copione|Musica chiesta|Didascalia|🎬|Sottotitoli:|📄)/u.test(t)) return "ai";
  return "";
}

// ---------------------------------------------------------------------------
// Console
// ---------------------------------------------------------------------------

function clearConsole() { $("console").innerHTML = ""; }
function resetConsole() {
  $("console").innerHTML = '<div class="console-empty">Il log della generazione apparirà qui.</div>';
  $("result-box").classList.remove("show");
  setStatus("in attesa");
}
function setStatus(t) { $("status-tag").textContent = t; }

function appendLine(text, cls) {
  const c = $("console");
  const first = c.querySelector(".console-empty");
  if (first) first.remove();
  const div = document.createElement("div");
  if (cls) div.className = `line-${cls}`;
  div.textContent = text;
  c.appendChild(div);
  c.scrollTop = c.scrollHeight;
}

function showResult(path, reportPath) {
  if (!path) return;
  $("result-title").textContent = "✓ Progetto creato";
  const pathEl = $("result-path");
  pathEl.textContent = path;
  pathEl.dataset.path = path;
  pathEl.dataset.report = reportPath || "";
  $("btn-report").hidden = !reportPath;
  $("btn-report").textContent = "Leggi il report delle decisioni";
  $("btn-open").textContent = "Apri cartella in Finder";
  $("result-box").classList.add("show");
}

function showBatchResult(results) {
  const ok = results.filter((r) => r.ok);
  const failed = results.filter((r) => !r.ok);
  $("result-title").textContent = `✓ ${ok.length}/${results.length} progetti creati`;
  const pathEl = $("result-path");
  let txt = ok.map((r) => "• " + r.name).join("\n");
  if (failed.length) txt += "\n\nFalliti:\n" + failed.map((r) => "✗ " + r.name).join("\n");
  pathEl.textContent = txt;
  // "open" opens the CapCut drafts folder (parent of any created project)
  const firstOk = ok[0] && ok[0].path;
  if (firstOk) {
    const parent = firstOk.replace(/[\/\\][^\/\\]+$/, "");
    pathEl.dataset.path = parent;
    $("btn-open").textContent = "Apri cartella progetti";
  }
  // one report per project: open their folder
  const firstReport = ok[0] && ok[0].report;
  pathEl.dataset.report = firstReport ? firstReport.replace(/[\/\\][^\/\\]+$/, "") : "";
  $("btn-report").hidden = !firstReport;
  $("btn-report").textContent = "Apri la cartella dei report";
  $("result-box").classList.add("show");
}

// ---------------------------------------------------------------------------
// AI wizard
// ---------------------------------------------------------------------------

function wireWizard() {
  $("btn-config-ai").onclick = openWizard;
  $("btn-ai-wizard-inline").onclick = openWizard;
  $("wizard-close").onclick = closeWizard;
  $("wizard-next").onclick = () => gotoStep(state.wizardStep + 1);
  $("wizard-prev").onclick = () => gotoStep(state.wizardStep - 1);
  $("wizard-done").onclick = () => {
    closeWizard();
    checkClaude();          // refresh the editor's Claude status + "Genera" gate
  };
  $("btn-ai-test").onclick = runAiTest;
}

function openWizard() {
  $("ai-wizard").hidden = false;
  gotoStep(1);
  loadAiStatus();
}
function closeWizard() { $("ai-wizard").hidden = true; }

function gotoStep(n) {
  state.wizardStep = Math.max(1, Math.min(3, n));
  document.querySelectorAll(".wizard-step").forEach((el) => {
    el.classList.toggle("active", Number(el.dataset.step) === state.wizardStep);
  });
  document.querySelectorAll(".wizard-pane").forEach((el) => {
    el.hidden = Number(el.dataset.pane) !== state.wizardStep;
  });
  $("wizard-prev").hidden = state.wizardStep === 1;
  $("wizard-next").hidden = state.wizardStep === 3;
  $("wizard-done").hidden = state.wizardStep !== 3;
}

async function loadAiStatus() {
  const box = $("ai-status-box");
  box.className = "status-block";
  box.textContent = "Controllo in corso…";
  try {
    const st = await (await fetch(`${BACKEND}/api/ai/status`)).json();
    if (st.available) {
      box.classList.add("ok");
      const via = st.method === "claude-cli" ? "Claude Code CLI" : "API key Anthropic";
      box.innerHTML = `✓ AI pronta — collegata via <strong>${via}</strong>. Puoi passare al test.`;
    } else {
      box.classList.add("warn");
      box.innerHTML = "⚠ AI non configurata. Segui il passo 2 per attivarla.";
    }
  } catch (e) {
    box.classList.add("warn");
    box.textContent = "Impossibile contattare il backend.";
  }
}

async function runAiTest() {
  const res = $("ai-test-result");
  res.className = "test-result";
  res.textContent = "Test in corso…";
  try {
    const r = await (await fetch(`${BACKEND}/api/ai/test`, { method: "POST" })).json();
    if (r.ok) {
      res.classList.add("ok");
      res.textContent = `✓ Funziona! Claude ha risposto: "${r.message}"`;
    } else {
      res.classList.add("err");
      res.textContent = `✗ ${r.message}`;
    }
  } catch (e) {
    res.classList.add("err");
    res.textContent = `✗ Errore: ${e}`;
  }
}

// ---------------------------------------------------------------------------
// Auth + subscription gate
// ---------------------------------------------------------------------------

async function getToken() {
  if (window.api && window.api.getToken) return window.api.getToken();
  return localStorage.getItem("capwiz_token");
}
async function setToken(t) {
  state.authToken = t;
  if (window.api && window.api.setToken) return window.api.setToken(t);
  localStorage.setItem("capwiz_token", t);
}
async function clearToken() {
  state.authToken = null;
  if (window.api && window.api.clearToken) return window.api.clearToken();
  localStorage.removeItem("capwiz_token");
}

function cacheActive(active) {
  localStorage.setItem("capwiz_sub", JSON.stringify({ active, ts: Date.now() }));
}
function cachedGraceOk() {
  try {
    const c = JSON.parse(localStorage.getItem("capwiz_sub") || "{}");
    if (!c.active) return false;
    return (Date.now() - c.ts) < OFFLINE_GRACE_DAYS * 86400 * 1000;
  } catch (_) { return false; }
}

async function startAuthGate() {
  const token = await getToken();
  if (!token) { showView("auth"); return; }
  state.authToken = token;
  await routeBySubscription();
}

// returns after routing to the correct view
async function routeBySubscription() {
  try {
    const r = await fetch(`${state.cloudUrl}/me`, {
      headers: { Authorization: `Bearer ${state.authToken}` },
    });
    if (r.status === 401) { await clearToken(); showView("auth"); return; }
    const me = await r.json();
    state.email = me.email;
    if (me.active) {
      cacheActive(true);
      showView("home");
    } else {
      cacheActive(false);
      renderSubInfo(me);
      showView("subscribe");
    }
  } catch (_) {
    // cloud unreachable — honor the offline grace period
    if (cachedGraceOk()) {
      showView("home");
    } else {
      renderSubInfo(null);
      showView("subscribe");
    }
  }
}

function renderSubInfo(me) {
  const el = $("sub-info");
  if (!me) {
    el.textContent = "Impossibile verificare l'abbonamento (offline). Riprova quando sei online.";
  } else if (me.subscription_status === "past_due") {
    el.textContent = "Pagamento in sospeso — aggiorna il metodo di pagamento.";
  } else if (me.subscription_status === "canceled") {
    el.textContent = "Abbonamento annullato. Riattivalo per continuare.";
  } else {
    el.textContent = "";
  }
}

function wireAuth() {
  $("auth-switch").onclick = () => {
    state.authMode = state.authMode === "login" ? "register" : "login";
    const login = state.authMode === "login";
    $("auth-title").textContent = login ? "Accedi" : "Registrati";
    $("auth-submit").textContent = login ? "Accedi" : "Crea account";
    $("auth-switch-text").textContent = login ? "Non hai un account?" : "Hai già un account?";
    $("auth-switch").textContent = login ? "Registrati" : "Accedi";
    $("auth-error").textContent = "";
  };
  $("auth-submit").onclick = doAuth;
  $("auth-password").onkeydown = (e) => { if (e.key === "Enter") doAuth(); };

  $("btn-subscribe").onclick = doSubscribe;
  $("btn-recheck").onclick = routeBySubscription;
  $("btn-logout-sub").onclick = doLogout;
  $("tb-logout").onclick = doLogout;
  $("tb-account").onclick = doManageBilling;
}

async function doAuth() {
  const email = $("auth-email").value.trim();
  const password = $("auth-password").value;
  const err = $("auth-error");
  err.textContent = "";
  if (!email || !password) { err.textContent = "Inserisci email e password."; return; }

  const path = state.authMode === "login" ? "/auth/login" : "/auth/register";
  $("auth-submit").disabled = true;
  try {
    const r = await fetch(`${state.cloudUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const data = await r.json();
    if (!r.ok) { err.textContent = data.detail || "Errore."; return; }
    await setToken(data.token);
    state.email = email;
    $("auth-password").value = "";
    await routeBySubscription();
  } catch (e) {
    err.textContent = `Impossibile contattare il server (${e}).`;
  } finally {
    $("auth-submit").disabled = false;
  }
}

async function doSubscribe() {
  try {
    const r = await fetch(`${state.cloudUrl}/billing/checkout`, {
      method: "POST",
      headers: { Authorization: `Bearer ${state.authToken}` },
    });
    const data = await r.json();
    if (r.ok && data.url) {
      openExternal(data.url);
    } else {
      $("sub-info").textContent = data.detail || "Impossibile avviare il checkout.";
    }
  } catch (e) {
    $("sub-info").textContent = `Errore: ${e}`;
  }
}

async function doManageBilling() {
  try {
    const r = await fetch(`${state.cloudUrl}/billing/portal`, {
      method: "POST",
      headers: { Authorization: `Bearer ${state.authToken}` },
    });
    const data = await r.json();
    if (r.ok && data.url) openExternal(data.url);
  } catch (_) {}
}

async function doLogout() {
  await clearToken();
  state.email = null;
  localStorage.removeItem("capwiz_sub");
  showView("auth");
}

function openExternal(url) {
  if (window.api && window.api.openExternal) window.api.openExternal(url);
  else window.open(url, "_blank");
}

// ---------------------------------------------------------------------------
// utils
// ---------------------------------------------------------------------------

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
