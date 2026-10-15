"""CLI: python -m agent run "<задача>" [--vision] [--model ...] [--dry-run] (P1-06)."""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys

from agent.config import load_settings
from security.allowlist import Allowlist


def _adb_devices() -> list[str]:
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [ln.split()[0] for ln in out.splitlines()[1:] if ln.strip().endswith("device")]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent", description="Hands-Free Phone Agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="выполнить задачу на телефоне")
    run.add_argument("task", help="задача на естественном языке")
    run.add_argument("--model", help="модель (по умолчанию LLM_MODEL из .env)")
    run.add_argument("--serial", help="серийный номер устройства (ANDROID_SERIAL)")
    run.add_argument("--vision", action="store_true", help="использовать скриншоты на всех экранах")
    run.add_argument("--steps", type=int, default=30, help="максимум шагов агента")
    run.add_argument("--max-cost", type=float, help="лимит стоимости задачи, $ (по умолчанию MAX_COST_USD)")
    run.add_argument("--dry-run", action="store_true", help="только проверить настройки, не трогая телефон")
    note = sub.add_parser("note", help="запомнить урок в карточке приложения (база знаний)")
    note.add_argument("package", help="пакет приложения, например kz.kaspi.mobile")
    note.add_argument("text", help="что нужно помнить при работе с этим приложением")
    sub.add_parser("notes", help="показать карточки базы знаний")
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)

    if args.cmd == "note":
        from agent.knowledge import add_lesson

        print(f"Запомнил в {add_lesson(args.package, args.text)}")
        return 0
    if args.cmd == "notes":
        from agent.knowledge import list_cards

        for package, lines in list_cards().items():
            print(f"{package}: {lines} строк")
        return 0

    settings = load_settings(args.model, args.serial)

    if args.dry_run:
        allow = Allowlist.from_yaml()
        devices = _adb_devices()
        print(f"Задача:      {args.task}")
        print(f"Модель:      {settings.model} ({settings.base_url})")
        print(f"API-ключ:    {settings.masked_key()}")
        from agent.pricing import price_per_million

        pin, pout, source = price_per_million(settings)
        print(f"Цена:        ${pin:g} / ${pout:g} за 1M токенов (вход/выход, источник: {source})")
        print(f"Лимит задачи: ${(args.max_cost if args.max_cost is not None else settings.max_cost_usd):g}")
        print(f"Vision:      {'да' if args.vision else 'нет (A11y-дерево)'}")
        print(f"Устройства:  {', '.join(devices) or 'не найдены'}")
        print(f"Allowlist:   {', '.join(sorted(allow.allowed))}")
        return 0 if settings.api_key and devices else 1

    from agent.runner import run_task

    report = asyncio.run(run_task(args.task, settings, vision=args.vision, steps=args.steps, max_cost=args.max_cost))
    print(f"\nСтатус:   {report.status}")
    print(f"Итог:     {report.reason}")
    print(f"Шагов:    {report.steps} · время {report.duration_s} с · запросов {report.requests}")
    print(f"Токены:   {report.input_tokens} вх / {report.output_tokens} исх · ~${report.cost_usd_estimate}")
    print(f"Журнал:   runs/{report.task_id}/")
    if report.status == "payment_stop":
        print(f"⏸ Остановка перед оплатой («{report.payment_label}»). Скриншот в журнале; нажмите кнопку вручную.")
    return 0 if report.status in ("success", "payment_stop") else 1


if __name__ == "__main__":
    raise SystemExit(main())
