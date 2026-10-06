"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("api", {
  backendUrl: () => ipcRenderer.invoke("backend-url"),
  cloudUrl: () => ipcRenderer.invoke("cloud-url"),
  selectFolder: () => ipcRenderer.invoke("select-folder"),
  selectVideo: () => ipcRenderer.invoke("select-video"),
  selectScript: () => ipcRenderer.invoke("select-script"),
  openPath: (p) => ipcRenderer.invoke("open-path", p),
  openExternal: (url) => ipcRenderer.invoke("open-external", url),
  getToken: () => ipcRenderer.invoke("auth-get-token"),
  setToken: (t) => ipcRenderer.invoke("auth-set-token", t),
  clearToken: () => ipcRenderer.invoke("auth-clear-token"),
  onBackendReady: (cb) =>
    ipcRenderer.on("backend-ready", (_e, data) => cb(data)),
});
