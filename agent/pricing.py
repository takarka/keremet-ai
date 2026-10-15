"""Реальные цены моделей из каталога OpenRouter (с кэшем на диске), иначе значения из .env."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from agent.config import ROOT, Settings

CACHE = ROOT / "data" / "pricing.json"
TTL_S = 24 * 3600


def _load_cache() -> dict:
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        if time.time() - data.get("fetched_at", 0) < TTL_S:
            return data["prices"]
    except (OSError, ValueError, KeyError):
        pass
    return {}


def _fetch(base_url: str) -> dict:
    resp = httpx.get(f"{base_url.rstrip('/')}/models", timeout=10)
    resp.raise_for_status()
    prices = {}
    for m in resp.json()["data"]:
        p = m.get("pricing") or {}
        try:
            prices[m["id"]] = [float(p["prompt"]) * 1e6, float(p["completion"]) * 1e6]
        except (KeyError, TypeError, ValueError):
            continue
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"fetched_at": time.time(), "prices": prices}), encoding="utf-8")
    return prices


def price_per_million(settings: Settings) -> tuple[float, float, str]:
    """(вход $/1M, выход $/1M, источник). Источник: openrouter | env."""
    prices = _load_cache()
    if settings.model not in prices and "openrouter" in settings.base_url:
        try:
            prices = _fetch(settings.base_url)
        except Exception:  # noqa: BLE001 — сеть недоступна: используем значения из .env
            prices = {}
    if settings.model in prices:
        pin, pout = prices[settings.model]
        return pin, pout, "openrouter"
    return settings.price_in, settings.price_out, "env"
