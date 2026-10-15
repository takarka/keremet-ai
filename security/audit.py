"""Audit Log: журнал траектории задачи в runs/<task_id>/ (P1-10)."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"


class AuditLog:
    def __init__(self, task_id: str | None = None, root: Path | str = RUNS_DIR) -> None:
        self.task_id = task_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.dir = Path(root) / self.task_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._actions = self.dir / "actions.jsonl"
        self._shots = 0

    def log(self, event: str, **data: Any) -> None:
        record = {"ts": time.time(), "event": event, **data}
        with self._actions.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def save_screenshot(self, label: str, png: bytes) -> Path:
        self._shots += 1
        path = self.dir / f"{self._shots:03d}-{label}.png"
        path.write_bytes(png)
        self.log("screenshot", file=path.name, label=label)
        return path
