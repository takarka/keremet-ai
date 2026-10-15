"""Единый конфигуратор LLM: любой OpenAI-совместимый провайдер (OpenRouter по умолчанию)."""

from __future__ import annotations

from agent.config import Settings


def build_llm(settings: Settings):
    from mobilerun.agent.utils.llm_picker import load_llm

    if not settings.api_key:
        raise RuntimeError("OPENROUTER_API_KEY не задан: заполните .env (см. .env.example)")
    return load_llm(
        "OpenAILike",
        model=settings.model,
        api_base=settings.base_url,
        api_key=settings.api_key,
        is_chat_model=True,
        is_function_calling_model=True,
    )
