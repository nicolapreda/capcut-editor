"""FastAPI backend — wraps the pipeline so a desktop UI (Electron) can drive it.

Design
------
* Generation is long-running and blocking, so each request spins a background
  thread. The pipeline's `log` callback appends lines to the job's buffer.
* A WebSocket streams those log lines live (plus a backlog for late joiners)
  and finishes with a status frame.
* File selection happens in the Electron layer via native dialogs; the UI sends
  us plain filesystem paths.

Run standalone for development:
    capwiz-server            # binds 127.0.0.1:8765
    capwiz-server --port 9000
"""
from __future__ import annotations

import argparse
import asyncio
import os
import platform
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import registry
from .draft import CAPCUT_PROJECTS_DIR
from .pipeline import read_script, run_pipeline


# ---------------------------------------------------------------------------
# Job manager
# ---------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    status: str = "pending"           # pending | running | done | error
    logs: list[str] = field(default_factory=list)
    result_path: str | None = None    # single-project result
    report_path: str | None = None    # AI decision report of the single project
    results: list[dict] = field(default_factory=list)  # batch results
    error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def log(self, line: str) -> None:
        with self._lock:
            self.logs.append(line)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id, "status": self.status,
                "logs": list(self.logs),
                "result_path": self.result_path, "report_path": self.report_path,
                "results": list(self.results), "error": self.error,
            }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self) -> Job:
        job = Job(id=uuid.uuid4().hex[:12])
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)


manager = JobManager()


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    input_path: str
    name: str
    template: str | None = None
    script_path: str | None = None
    model: str = "large-v3"
    language: str = "it"
    pacing: str = "fast"
    stabilize_clips: bool = False
    redistribute_sfx_enabled: bool = True
    add_emphasis: bool = True
    emphasis_count: int = 4
    drop_fillers: bool = True
    aggressive_fillers: bool = False
    intro_title: str | None = None
    clear_template_texts: bool = False
    ai_model: str = "sonnet"
    use_ai_review: bool = True
    ai_target_duration: float | None = None
    subtitle_max_words: int = 4
    subtitle_max_duration: float = 1.4
    subtitle_like_template: bool = True
    fmt: str = "vertical"
    projects_dir: str | None = None


class BatchRequest(GenerateRequest):
    # In batch mode input_path/name are derived per subfolder, so make them
    # optional and add the batch-specific fields.
    input_path: str = ""
    name: str = ""
    parent_folder: str
    name_prefix: str = ""


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="CapWiz API", version="0.1.0")

# The Electron renderer runs from a file:// or localhost origin; allow all in
# this local-only server (it binds 127.0.0.1, never exposed to the network).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": app.version}


def _whisper_cached_models() -> list[str]:
    """Best-effort list of already-downloaded faster-whisper models."""
    hub = Path.home() / ".cache/huggingface/hub"
    if not hub.exists():
        return []
    found = []
    for d in hub.iterdir():
        n = d.name.lower()
        if "faster-whisper" in n or "whisper" in n:
            # models--Systran--faster-whisper-large-v3 → large-v3
            found.append(d.name.split("faster-whisper-")[-1] if "faster-whisper-" in d.name else d.name)
    return found


@app.get("/api/system")
def system() -> dict:
    from .ai import _find_claude_cli
    return {
        "os": platform.system().lower(),          # darwin | windows | linux
        "os_version": platform.version(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "whisper_models_cached": _whisper_cached_models(),
        "ai": {
            "claude_cli": _find_claude_cli(),
            "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        },
        "projects_dir": str(CAPCUT_PROJECTS_DIR),
        "projects_dir_exists": CAPCUT_PROJECTS_DIR.exists(),
    }


@app.get("/api/templates")
def templates(projects_dir: str | None = None) -> dict:
    base = Path(projects_dir) if projects_dir else CAPCUT_PROJECTS_DIR
    items: list[dict] = []
    if base.exists():
        for d in sorted(base.iterdir()):
            if d.is_dir() and (d / "draft_info.json").exists():
                items.append({"name": d.name})
    default = next((t["name"] for t in items if t["name"].upper() == "CASA RIFUGIO"), None)
    return {"projects_dir": str(base), "default": default, "templates": items}


def _do_generate(req: GenerateRequest, log) -> tuple[str, str]:
    """Run the pipeline for one project and record it.
    Returns (draft path, AI decision report path)."""
    script_text = read_script(Path(req.script_path)) if req.script_path else None
    res = run_pipeline(
        input_path=Path(req.input_path),
        name=req.name,
        template=req.template,
        script=script_text,
        model=req.model,
        language=req.language,
        pacing=req.pacing,
        stabilize_clips=req.stabilize_clips,
        redistribute_sfx_enabled=req.redistribute_sfx_enabled,
        add_emphasis=req.add_emphasis,
        emphasis_count=req.emphasis_count,
        drop_fillers=req.drop_fillers,
        aggressive_fillers=req.aggressive_fillers,
        intro_title=req.intro_title,
        clear_template_texts=req.clear_template_texts,
        ai_model=req.ai_model,
        use_ai_review=req.use_ai_review,
        ai_target_duration=req.ai_target_duration,
        subtitle_max_words=req.subtitle_max_words,
        subtitle_max_duration=req.subtitle_max_duration,
        subtitle_like_template=req.subtitle_like_template,
        fmt=req.fmt,
        projects_dir=Path(req.projects_dir) if req.projects_dir else None,
        log=log,
    )
    try:
        registry.add_project(name=req.name, result_path=str(res.draft_path),
                             settings=req.model_dump(), report_path=str(res.report_path))
    except Exception as reg_err:  # noqa: BLE001 — registry is best-effort
        log(f"⚠ impossibile salvare nel registro progetti: {reg_err}")
    return str(res.draft_path), str(res.report_path)


def _run_job(job: Job, req: GenerateRequest) -> None:
    job.status = "running"
    try:
        job.result_path, job.report_path = _do_generate(req, job.log)
        job.status = "done"
    except Exception as e:  # noqa: BLE001 — surface everything to the UI
        job.error = str(e)
        job.status = "error"
        job.log(f"ERRORE: {e}")


def _project_subfolders(parent: Path) -> list[Path]:
    """Immediate subfolders of `parent` that contain at least one video."""
    from .pipeline import _is_video
    if not parent.is_dir():
        return []
    out: list[Path] = []
    for d in sorted(parent.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        try:
            if any(_is_video(p) for p in d.iterdir()):
                out.append(d)
        except OSError:
            continue
    return out


def _run_batch(job: Job, req: BatchRequest) -> None:
    job.status = "running"
    try:
        parent = Path(req.parent_folder)
        subs = _project_subfolders(parent)
        if not subs:
            job.error = "Nessuna sottocartella con video trovata."
            job.status = "error"
            job.log(f"ERRORE: {job.error}")
            return

        job.log(f"▶ Modalità blocco: {len(subs)} progetti da generare")
        # fail fast: without Claude every project would fail the same way
        from .ai import require_ai
        require_ai(req.ai_model, job.log)
        base = req.model_dump()
        base.pop("parent_folder", None)
        base.pop("name_prefix", None)

        for i, sub in enumerate(subs, 1):
            name = (f"{req.name_prefix} {sub.name}".strip()
                    if req.name_prefix else sub.name)
            job.log(f"\n▶▶ PROGETTO [{i}/{len(subs)}] {name}  ←  {sub.name}")
            try:
                one = GenerateRequest(**{**base, "input_path": str(sub), "name": name})
                out, report = _do_generate(one, job.log)
                job.results.append({"name": name, "path": out, "report": report, "ok": True})
            except Exception as e:  # one folder failing must not abort the batch
                job.log(f"ERRORE su «{name}»: {e}")
                job.results.append({"name": name, "path": None, "ok": False, "error": str(e)})

        ok = sum(1 for r in job.results if r["ok"])
        job.log(f"\n✓ Blocco completato: {ok}/{len(subs)} progetti creati")
        job.status = "done"
    except Exception as e:  # noqa: BLE001
        job.error = str(e)
        job.status = "error"
        job.log(f"ERRORE: {e}")


@app.post("/api/generate")
def generate(req: GenerateRequest) -> dict:
    job = manager.create()
    t = threading.Thread(target=_run_job, args=(job, req), daemon=True)
    t.start()
    return {"job_id": job.id}


@app.get("/api/subfolders")
def subfolders(path: str) -> dict:
    subs = _project_subfolders(Path(path))
    return {"folders": [{"name": s.name, "path": str(s)} for s in subs]}


@app.post("/api/batch")
def batch(req: BatchRequest) -> dict:
    job = manager.create()
    t = threading.Thread(target=_run_batch, args=(job, req), daemon=True)
    t.start()
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        return {"error": "job non trovato"}
    return job.snapshot()


@app.get("/api/projects")
def projects_list() -> dict:
    return {"projects": registry.list_projects()}


@app.get("/api/projects/{project_id}")
def project_get(project_id: str) -> dict:
    p = registry.get_project(project_id)
    return p or {"error": "progetto non trovato"}


@app.delete("/api/projects/{project_id}")
def project_delete(project_id: str) -> dict:
    return {"removed": registry.remove_project(project_id)}


@app.get("/api/ai/status")
def ai_status_endpoint() -> dict:
    from .ai import ai_status
    return ai_status()


@app.post("/api/ai/test")
def ai_test_endpoint() -> dict:
    from .ai import ai_ping
    return ai_ping()


@app.websocket("/api/ws/jobs/{job_id}")
async def job_logs(websocket: WebSocket, job_id: str) -> None:
    await websocket.accept()
    job = manager.get(job_id)
    if not job:
        await websocket.send_json({"type": "error", "message": "job non trovato"})
        await websocket.close()
        return

    sent = 0
    try:
        while True:
            # flush any new log lines
            snapshot_logs = job.logs  # atomic read of the list reference
            while sent < len(snapshot_logs):
                await websocket.send_json({"type": "log", "line": snapshot_logs[sent]})
                sent += 1
            if job.status in ("done", "error"):
                await websocket.send_json({
                    "type": "status", "status": job.status,
                    "result_path": job.result_path,
                    "report_path": job.report_path,
                    "results": job.results,
                    "error": job.error,
                })
                break
            await asyncio.sleep(0.15)
    except WebSocketDisconnect:
        return
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    parser = argparse.ArgumentParser(description="CapWiz backend server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    run()
