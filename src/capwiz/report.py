"""Decision report: every line goes to the live log AND to a Markdown file.

The file lets the user re-read, after the run, exactly what the AI decided and
why (cuts, silences, behind-the-scenes, sounds, texts), plus an annotated
transcript. Saved under the app-data dir, one file per generated project.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from .registry import app_data_dir


def reports_dir() -> Path:
    d = app_data_dir() / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w\- ]+", "_", name).strip() or "progetto"


class Report:
    """Callable logger that also records a Markdown report."""

    def __init__(self, title: str, log: Callable[[str], None]):
        self.title = title
        self._log = log
        self._lines: list[str] = []
        self._appendix: list[str] = []
        self.started = time.time()

    # -- live + file ---------------------------------------------------------

    def __call__(self, line: str = "") -> None:
        self._log(line)
        self._lines.append(line)

    def section(self, title: str) -> None:
        self("")
        self(f"━━ {title} ━━")

    # -- file only -----------------------------------------------------------

    def appendix(self, md: str) -> None:
        self._appendix.append(md)

    # -- output --------------------------------------------------------------

    def save(self) -> Path:
        stamp = time.strftime("%Y-%m-%d %H.%M", time.localtime(self.started))
        path = reports_dir() / f"{_safe_name(self.title)} — {stamp}.md"
        elapsed = time.time() - self.started
        md = [
            f"# Report decisioni AI — {self.title}",
            "",
            f"Generato il {time.strftime('%d/%m/%Y alle %H:%M', time.localtime(self.started))}"
            f" · durata elaborazione {elapsed:.0f}s",
            "",
            "## Log completo",
            "",
            "```text",
            *self._lines,
            "```",
        ]
        if self._appendix:
            md += ["", *self._appendix]
        path.write_text("\n".join(md) + "\n", encoding="utf-8")
        return path
