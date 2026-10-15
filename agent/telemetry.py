"""Телеметрия запуска: JSON-отчёт, из которого считаются KPI (P1-08)."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class RunReport:
    task_id: str
    task: str
    model: str
    vision: bool
    status: str = "error"  # success | failed | payment_stop | stuck | budget_exceeded | error
    reason: str = ""
    steps: int = 0
    duration_s: float = 0.0
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd_estimate: float = 0.0
    payment_label: str | None = None
    approval_pending: bool = False
    max_cost_usd: float = 0.0
    price_source: str = ""
    started_at: float = field(default_factory=time.time)

    def estimate_cost(self, price_in: float, price_out: float) -> None:
        self.cost_usd_estimate = round(
            self.input_tokens / 1e6 * price_in + self.output_tokens / 1e6 * price_out, 5
        )

    def save(self, directory: Path) -> Path:
        path = Path(directory) / "report.json"
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        return path
