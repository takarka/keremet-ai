"""Запуск задачи: mobilerun-агент поверх GuardedDriver (P1-06, P1-07)."""

from __future__ import annotations

import asyncio
import time

from agent.config import ROOT, Settings
from agent.knowledge import with_knowledge
from agent.llm import build_llm
from agent.pricing import price_per_million
from agent.telemetry import RunReport
from security.allowlist import Allowlist
from security.approval import ApprovalSession
from security.audit import AuditLog
from security.guarded_driver import BudgetExceeded, GuardedDriver, TaskTimeout
from security.payment_guard import PaymentApprovalRequired

KNOWLEDGE_DIR = ROOT / "knowledge" / "app_cards"


def cost_usd(input_tokens: int, output_tokens: int, price_in: float, price_out: float) -> float:
    return input_tokens / 1e6 * price_in + output_tokens / 1e6 * price_out


async def _cancel(handler) -> None:
    """Отменить запущенный workflow; cancel_run — корутина, ошибки отмены не должны ронять отчёт."""
    try:
        await handler.cancel_run()
    except Exception:  # noqa: BLE001
        pass


async def run_task(
    task: str,
    settings: Settings,
    vision: bool = False,
    steps: int = 30,
    max_cost: float | None = None,
) -> RunReport:
    from mobilerun import MobileAgent
    from mobilerun.agent.usage import track_usage
    from mobilerun.config_manager import ConfigLoader
    from mobilerun.tools.driver import AndroidDriver

    limit = settings.max_cost_usd if max_cost is None else max_cost
    price_in, price_out, price_source = price_per_million(settings)

    audit = AuditLog()
    report = RunReport(task_id=audit.task_id, task=task, model=settings.model, vision=vision)
    report.max_cost_usd = limit
    report.price_source = price_source
    audit.log("task_start", task=task, model=settings.model, vision=vision, steps=steps,
              max_cost=limit, price_in=price_in, price_out=price_out, price_source=price_source)
    started = time.monotonic()
    guarded: GuardedDriver | None = None
    tracker = None
    try:
        inner = AndroidDriver(serial=settings.serial)
        await inner.connect()
        guarded = GuardedDriver(inner, Allowlist.from_yaml(), audit)

        llm = build_llm(settings)
        tracker = track_usage(llm)

        config = ConfigLoader.load(None)
        config.agent.max_steps = steps
        config.agent.app_cards.app_cards_dir = str(KNOWLEDGE_DIR)  # база знаний: карточки приложений
        if vision:
            config.agent.manager.vision = True
            config.agent.executor.vision = True
            config.agent.fast_agent.vision = True

        goal, used_cards = with_knowledge(task)
        audit.log("knowledge", cards=used_cards, goal_chars=len(goal))
        agent = MobileAgent(goal=goal, llms=llm, config=config, driver=guarded, timeout=1000)
        handler = agent.run()

        async def drive() -> None:
            async for _ in handler.stream_events():
                usage = tracker.usage
                spent = cost_usd(usage.request_tokens, usage.response_tokens, price_in, price_out)
                if limit and spent > limit and guarded.stuck is None:
                    guarded.abort(BudgetExceeded(f"Превышен лимит стоимости ${limit:.2f} (потрачено ~${spent:.2f})"))
                    await _cancel(handler)
                    return

        try:
            await asyncio.wait_for(drive(), timeout=settings.max_seconds or None)
        except asyncio.TimeoutError:
            guarded.abort(TaskTimeout(f"Превышено время {settings.max_seconds:g} с (возможно, завис Portal или приложение)"))
            await _cancel(handler)
        if guarded.stuck is None:
            result = await handler
            report.status = "success" if result.success else "failed"
            report.reason = str(result.reason)
            report.steps = int(result.steps)
    except PaymentApprovalRequired as stop:
        report.status = "payment_stop"
        report.reason = str(stop)
    except Exception as exc:  # noqa: BLE001 — отчёт нужен при любом сбое
        report.status = "error"
        report.reason = f"{type(exc).__name__}: {exc}"
        audit.log("task_error", error=report.reason)

    # Остановку мог поймать сам агент как ошибку действия: проверяем флаг драйвера.
    if guarded is not None and guarded.halted is not None:
        report.status = "payment_stop"
        report.payment_label = guarded.halted.label
        report.reason = str(guarded.halted)
        shot = next(audit.dir.glob("*-payment-stop.png"), None)
        session = ApprovalSession(audit.task_id)
        session.request(
            guarded.halted.category,
            guarded.halted.label,
            guarded.halted.x or 0,
            guarded.halted.y or 0,
            shot.name if shot else None,
        )
        session.save(audit.dir)
        report.approval_pending = True

    if guarded is not None and guarded.stuck is not None and report.status != "payment_stop":
        report.status = (
            "budget_exceeded" if isinstance(guarded.stuck, BudgetExceeded)
            else "timeout" if isinstance(guarded.stuck, TaskTimeout)
            else "stuck"
        )
        report.reason = str(guarded.stuck)

    if tracker is not None:
        usage = tracker.usage
        report.requests = usage.requests
        report.input_tokens = usage.request_tokens
        report.output_tokens = usage.response_tokens
        report.cost_usd_estimate = round(cost_usd(usage.request_tokens, usage.response_tokens, price_in, price_out), 5)

    report.duration_s = round(time.monotonic() - started, 1)
    audit.log("task_end", status=report.status, reason=report.reason)
    report.save(audit.dir)
    return report
