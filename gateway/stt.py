"""Распознавание голосовых сообщений через OpenAI-совместимый /audio/transcriptions (P2-04, P2-05).

Подходит Whisper API (OpenAI) и любые совместимые провайдеры. Ключ и URL — из .env.
"""

from __future__ import annotations

import os

import httpx

from agent.config import ROOT  # noqa: F401  (гарантирует загрузку .env)

# Подсказка Whisper: смешанный ru/kk/en и локальные названия лучше распознаются с контекстом.
DEFAULT_PROMPT = (
    "Команды для телефона на русском, казахском и английском. Названия: Kaspi, Kaspi Travel, 2ГИС, "
    "Wolt, Яндекс Go, WhatsApp, Telegram, Алматы, Астана, Шымкент, Қарағанды."
)


class SttError(Exception):
    pass


async def transcribe(
    audio: bytes,
    filename: str = "voice.ogg",
    *,
    client: httpx.AsyncClient | None = None,
    language: str | None = None,
) -> str:
    key = os.environ.get("STT_API_KEY", "").strip()
    if not key:
        raise SttError("STT_API_KEY не задан (см. .env.example)")
    base = os.environ.get("STT_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("STT_MODEL", "whisper-1")
    data = {"model": model, "prompt": os.environ.get("STT_PROMPT", DEFAULT_PROMPT)}
    if language or os.environ.get("STT_LANGUAGE"):
        data["language"] = language or os.environ["STT_LANGUAGE"]

    own = client is None
    client = client or httpx.AsyncClient(timeout=60)
    try:
        resp = await client.post(
            f"{base}/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            data=data,
            files={"file": (filename, audio, "application/octet-stream")},
        )
    finally:
        if own:
            await client.aclose()
    if resp.status_code != 200:
        raise SttError(f"STT вернул {resp.status_code}: {resp.text[:200]}")
    text = (resp.json().get("text") or "").strip()
    if not text:
        raise SttError("Речь не распознана")
    return text
