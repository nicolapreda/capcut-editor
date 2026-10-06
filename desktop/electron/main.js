"use strict";

const { app, BrowserWindow, ipcMain, dialog, shell, safeStorage } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");

// Fixed local port. The backend binds 127.0.0.1 only (never exposed).
const BACKEND_PORT = 8765;
const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`;

// Cloud licensing backend (accounts + Stripe subscription check).
// Override with CAPCUT_CLOUD_URL; defaults to local dev server.
const CLOUD_URL = process.env.CAPCUT_CLOUD_URL || "http://127.0.0.1:8799";

let pyProc = null;
let mainWindow = null;
let quitting = false;

// One copy of the app at a time. A second one couldn't start its own backend
// (the port is taken) and would silently talk to the first one's — until that
// one is closed, and then every request fails.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
}

// ---------------------------------------------------------------------------
// Locating the Python backend
//
// DEV: run from the repo — use the project's virtualenv interpreter and launch
//      the FastAPI server module.
// PROD (packaged): a PyInstaller-built sidecar binary is shipped in resources/.
//      (Wired later when we set up electron-builder + PyInstaller.)
// ---------------------------------------------------------------------------

function repoRoot() {
  // desktop/electron/main.js → repo root is two levels up
  return path.resolve(__dirname, "..", "..");
}

function devPythonPath() {
  const root = repoRoot();
  return process.platform === "win32"
    ? path.join(root, ".venv", "Scripts", "python.exe")
    : path.join(root, ".venv", "bin", "python");
}

function startBackend() {
  if (app.isPackaged) {
    // Packaged sidecar (PyInstaller onefile) lives in resources/backend/
    const exe = process.platform === "win32" ? "capcut-auto-server.exe" : "capcut-auto-server";
    const bin = path.join(process.resourcesPath, "backend", exe);
    pyProc = spawn(bin, ["--port", String(BACKEND_PORT)], { env: { ...process.env } });
  } else {
    pyProc = spawn(
      devPythonPath(),
      ["-m", "capcut_auto.server", "--port", String(BACKEND_PORT)],
      { cwd: repoRoot(), env: { ...process.env } }
    );
  }
  pyProc.stdout.on("data", (d) => console.log("[backend]", d.toString().trimEnd()));
  pyProc.stderr.on("data", (d) => console.log("[backend]", d.toString().trimEnd()));
  pyProc.on("exit", (code) => {
    console.log("[backend] exited with code", code);
    // 0 / null = stopped on purpose or by a signal (Ctrl+C reaches it directly)
    if (quitting || code === 0 || code === null) return;
    // The engine died while the app is open: say so instead of leaving a window
    // whose every button fails with "Failed to fetch".
    dialog.showErrorBox(
      "Il motore dell'app si è fermato",
      `Il backend Python non è in esecuzione (codice ${code}).\n\n` +
        `Di solito succede quando la porta ${BACKEND_PORT} è occupata da un'altra copia ` +
        "dell'app rimasta aperta. Chiudi tutte le finestre di CapCut Auto e riavvia.\n\n" +
        "I dettagli sono nel terminale da cui hai lanciato l'app."
    );
  });
  pyProc.on("error", (err) => console.error("[backend] spawn error", err));
}

async function waitForBackend(timeoutMs = 60000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const r = await fetch(`${BACKEND_URL}/api/health`);
      if (r.ok) return true;
    } catch (_) {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1120,
    height: 840,
    minWidth: 940,
    minHeight: 720,
    backgroundColor: "#0e0f13",
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.loadFile(path.join(__dirname, "..", "renderer", "index.html"));
}

app.whenReady().then(async () => {
  if (!app.hasSingleInstanceLock()) return;
  startBackend();
  createWindow();

  const ok = await waitForBackend();
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("backend-ready", { ok, url: BACKEND_URL });
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  quitting = true;
});

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    quitting = true;
    app.quit();
  });
}

app.on("quit", () => {
  if (pyProc) {
    try {
      pyProc.kill();
    } catch (_) {
      /* already gone */
    }
  }
});

// ---------------------------------------------------------------------------
// IPC — native dialogs + helpers the renderer can't do itself
// ---------------------------------------------------------------------------

ipcMain.handle("backend-url", () => BACKEND_URL);

ipcMain.handle("select-folder", async () => {
  const r = await dialog.showOpenDialog(mainWindow, { properties: ["openDirectory"] });
  return r.canceled ? null : r.filePaths[0];
});

ipcMain.handle("select-video", async () => {
  const r = await dialog.showOpenDialog(mainWindow, {
    properties: ["openFile"],
    filters: [{ name: "Video", extensions: ["mp4", "mov", "mkv", "webm", "m4v"] }],
  });
  return r.canceled ? null : r.filePaths[0];
});

ipcMain.handle("select-script", async () => {
  const r = await dialog.showOpenDialog(mainWindow, {
    properties: ["openFile"],
    filters: [{ name: "Script", extensions: ["txt", "md", "docx"] }],
  });
  return r.canceled ? null : r.filePaths[0];
});

ipcMain.handle("open-path", async (_e, p) => {
  if (p) await shell.openPath(p);
});

ipcMain.handle("open-external", async (_e, url) => {
  if (url) await shell.openExternal(url);
});

ipcMain.handle("cloud-url", () => CLOUD_URL);

// ---- auth token: encrypted at rest via the OS keychain (safeStorage) -------

function tokenFile() {
  return path.join(app.getPath("userData"), "auth.bin");
}

ipcMain.handle("auth-get-token", () => {
  try {
    const f = tokenFile();
    if (!fs.existsSync(f)) return null;
    const buf = fs.readFileSync(f);
    if (safeStorage.isEncryptionAvailable()) {
      return safeStorage.decryptString(buf);
    }
    return buf.toString("utf-8");   // fallback (unencrypted) if keychain unavailable
  } catch (_) {
    return null;
  }
});

ipcMain.handle("auth-set-token", (_e, token) => {
  try {
    const data = safeStorage.isEncryptionAvailable()
      ? safeStorage.encryptString(token)
      : Buffer.from(token, "utf-8");
    fs.writeFileSync(tokenFile(), data);
    return true;
  } catch (_) {
    return false;
  }
});

ipcMain.handle("auth-clear-token", () => {
  try {
    const f = tokenFile();
    if (fs.existsSync(f)) fs.unlinkSync(f);
    return true;
  } catch (_) {
    return false;
  }
});
