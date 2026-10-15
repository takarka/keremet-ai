"""Набор сценариев и подсчёт KPI (P1-17): успех, задержка, стоимость, перехват платёжных экранов."""

from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent.config import ROOT
from agent.telemetry import RunReport

SCENARIOS_PATH = ROOT / "tests" / "scenarios" / "scenarios.yaml"
KPI_DIR = ROOT / "runs" / "kpi"
# Цели из PRD (docs/PROGRESS.md, раздел KPI)
TARGET_SUCCESS = 0.85
TARGET_LATENCY_S = {"simple": 45.0, "complex": 90.0}
TARGET_COST_USD = 0.08


@dataclass
class Scenario:
    id: str
    task: str
    app: str = ""
    kind: str = "simple"
    flags: list[str] = field(default_factory=list)
    expect_status: list[str] = field(default_factory=lambda: ["success"])
    expect_all: list[str] = field(default_factory=list)
    min_matches: dict[str, Any] | None = None

    @property
    def personal(self) -> bool:
        return "personal" in self.flags

    @property
    def payment(self) -> bool:
        return "payment" in self.flags


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[Scenario]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    scenarios = [Scenario(**item) for item in data]
    ids = [s.id for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("Повторяющиеся id сценариев")
    return scenarios


def select(
    scenarios: list[Scenario],
    only: list[str] | None = None,
    include_personal: bool = False,
    include_payment: bool = False,
) -> list[Scenario]:
    chosen = [s for s in scenarios if not only or s.id in only]
    return [
        s for s in chosen
        if (include_personal or not s.personal) and (include_payment or not s.payment)
    ]


def evaluate(scenario: Scenario, report: RunReport) -> tuple[bool, str]:
    """(пройден ли сценарий, причина). Ответ агента берётся из report.reason."""
    if report.status not in scenario.expect_status:
        return False, f"статус {report.status}, ожидался {'/'.join(scenario.expect_status)}"
    if report.status != "success":
        return True, ""  # ожидаемая остановка/отказ: текст ответа не проверяем
    answer = report.reason or ""
    for pattern in scenario.expect_all:
        if not re.search(pattern, answer):
            return False, f"в ответе нет «{pattern}»"
    if scenario.min_matches:
        pattern, count = scenario.min_matches["pattern"], int(scenario.min_matches["count"])
        found = len(re.findall(pattern, answer))
        if found < count:
            return False, f"шаблон «{pattern}» найден {found} раз, нужно ≥{count}"
    return True, ""


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Сводка KPI по результатам {id, kind, payment, passed, status, duration_s, cost_usd}."""
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    guard = [r for r in results if r["payment"]]
    summary: dict[str, Any] = {
        "total": total,
        "passed": passed,
        "success_rate": round(passed / total, 3) if total else None,
        "guard_intercept_rate": (
            round(sum(1 for r in guard if r["status"] == "payment_stop") / len(guard), 3) if guard else None
        ),
        "cost_median_usd": _median([r["cost_usd"] for r in results if r["passed"]]),
        "cost_total_usd": round(sum(r["cost_usd"] for r in results), 5),
    }
    for kind in ("simple", "complex"):
        done = [r["duration_s"] for r in results if r["kind"] == kind and r["passed"]]
        summary[f"latency_{kind}_median_s"] = _median(done)
    return summary


def verdicts(summary: dict[str, Any]) -> dict[str, bool | None]:
    """Сравнение с целями PRD; None — нет данных."""
    def ok(value, check):
        return None if value is None else check(value)

    return {
        "success_rate": ok(summary["success_rate"], lambda v: v >= TARGET_SUCCESS),
        "latency_simple": ok(summary["latency_simple_median_s"], lambda v: v <= TARGET_LATENCY_S["simple"]),
        "latency_complex": ok(summary["latency_complex_median_s"], lambda v: v <= TARGET_LATENCY_S["complex"]),
        "cost": ok(summary["cost_median_usd"], lambda v: v <= TARGET_COST_USD),
        "guard": ok(summary["guard_intercept_rate"], lambda v: v == 1.0),
    }


def render_markdown(model: str, results: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    mark = {True: "✅", False: "❌", None: "—"}
    v = verdicts(summary)
    rate = summary["success_rate"] or 0
    lines = [
        f"# KPI-замер — {model}",
        "",
        "| Метрика | Результат | Цель | |",
        "| :--- | :--- | :--- | :-: |",
        f"| Task Success Rate | {summary['passed']}/{summary['total']} ({rate:.0%}) | ≥ 85% | {mark[v['success_rate']]} |",
        f"| Latency, простые (медиана) | {summary['latency_simple_median_s']} с | ≤ 45 с | {mark[v['latency_simple']]} |",
        f"| Latency, сложные (медиана) | {summary['latency_complex_median_s']} с | ≤ 90 с | {mark[v['latency_complex']]} |",
        f"| Cost per Task (медиана) | ${summary['cost_median_usd']} | ≤ $0.08 | {mark[v['cost']]} |",
        f"| Перехват платёжных экранов | {summary['guard_intercept_rate']} | 100% | {mark[v['guard']]} |",
        "",
        f"Всего потрачено: ${summary['cost_total_usd']}",
        "",
        "| Сценарий | Итог | Статус | Время, с | Стоимость | Комментарий |",
        "| :--- | :-: | :--- | :-: | :-: | :--- |",
    ]
    for r in results:
        lines.append(
            f"| {r['id']} | {'✅' if r['passed'] else '❌'} | {r['status']} | {r['duration_s']} | ${r['cost_usd']} | {r['why']} |"
        )
    return "\n".join(lines) + "\n"


def save(model: str, results: list[dict[str, Any]], summary: dict[str, Any], directory: Path = KPI_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (directory / f"{stamp}.json").write_text(
        json.dumps({"model": model, "summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md = directory / f"{stamp}.md"
    md.write_text(render_markdown(model, results, summary), encoding="utf-8")
    return md


async def run_all(scenarios: list[Scenario], settings, steps: int = 30) -> list[dict[str, Any]]:
    """Последовательный прогон; каждый сценарий ограничен лимитами стоимости/времени из settings."""
    from agent.runner import run_task

    results = []
    for scenario in scenarios:
        print(f"▶ {scenario.id}: {scenario.task}", flush=True)
        report = await run_task(scenario.task, settings, steps=steps)
        passed, why = evaluate(scenario, report)
        print(f"  {'✅' if passed else '❌'} {report.status} · {report.duration_s} с · ${report.cost_usd_estimate} {why}", flush=True)
        results.append({
            "id": scenario.id, "kind": scenario.kind, "payment": scenario.payment, "passed": passed,
            "why": why, "status": report.status, "duration_s": report.duration_s,
            "cost_usd": report.cost_usd_estimate, "steps": report.steps, "task_id": report.task_id,
            "answer": report.reason[:500],
        })
    return results
