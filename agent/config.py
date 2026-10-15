"""Настройки из переменных окружения / .env (P1-05). Ключи в код не попадают."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    serial: str | None
    # Оценка стоимости, $ за 1M токенов (OpenRouter не отдаёт цену в usage mobilerun).
    price_in: float
    price_out: float
    max_cost_usd: float  # жёсткий лимит стоимости одной задачи, $ (0 = без лимита)
    max_seconds: float  # лимит времени одной задачи, с (0 = без лимита)
    decider_model: str = ""  # decision-модель второго слоя защиты (пусто = выключен)
    decider_threshold: float = 0.5

    def masked_key(self) -> str:
        return f"…{self.api_key[-4:]}" if len(self.api_key) > 8 else "(не задан)"


def load_settings(model: str | None = None, serial: str | None = None) -> Settings:
    return Settings(
        api_key=os.environ.get("OPENROUTER_API_KEY", "").strip(),
        base_url=os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1").strip(),
        model=(model or os.environ.get("LLM_MODEL") or "anthropic/claude-sonnet-4.5").strip(),
        serial=(serial or os.environ.get("ANDROID_SERIAL") or "").strip() or None,
        price_in=float(os.environ.get("LLM_PRICE_IN_PER_M", "3.0")),
        price_out=float(os.environ.get("LLM_PRICE_OUT_PER_M", "15.0")),
        max_cost_usd=float(os.environ.get("MAX_COST_USD", "0.25")),
        max_seconds=float(os.environ.get("MAX_SECONDS", "300")),
        decider_model=os.environ.get("DECIDER_MODEL", "").strip(),
        decider_threshold=float(os.environ.get("DECIDER_THRESHOLD", "0.5")),
    )
