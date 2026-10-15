"""Второй слой Payment Guard: decision-модель (Jev / Perplexity Decider) оценивает кнопку под тапом.

Только добавляет остановки: regex-защита остаётся основной и работает fail-closed.
Сбой, таймаут или недоступность Decisions API никогда не блокируют и не разрешают действие сами по себе —
решает regex. Данные (подпись кнопки + тексты экрана) уходят в облако так же, как и в LLM-агента.
"""

from __future__ import annotations

from typing import Any

import httpx

from security.approval import Category

URL = "https://openrouter.ai/api/alpha/decisions"

_QUESTIONS = {
    Category.FINANCE: {
        "type": "noul",
        "instructions": "The element under the user's finger is a button or link. Would tapping it immediately pay, buy, transfer money or confirm a purchase?",
        "criteria": {
            "true": "Tapping completes a payment, purchase, transfer or order confirmation.",
            "false": "Tapping only navigates, searches, adds to a cart, shows information or offers installments without paying.",
        },
    },
    Category.DELETE: {
        "type": "noul",
        "instructions": "Would tapping this element delete or irreversibly remove data (chat, message, account, order)?",
        "criteria": {"true": "Deletes or removes data.", "false": "Does not delete anything."},
    },
}


class DeciderGuard:
    def __init__(
        self,
        api_key: str,
        model: str = "typesafe/jev-1.14",
        threshold: float = 0.5,
        timeout: float = 4.0,
        client: Any = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.threshold = threshold
        self.timeout = timeout
        self._client = client or httpx.AsyncClient()
        self._cache: dict[tuple, tuple[Category, float] | None] = {}

    async def check(
        self, texts: list[str], context: str, categories: frozenset[Category] | set[Category]
    ) -> tuple[Category, str, float] | None:
        """(категория, подпись, вероятность) если модель считает действие рискованным, иначе None. Бросает при сбое API."""
        label = " / ".join(texts)[:200]
        asked = {c: q for c, q in _QUESTIONS.items() if c in categories}
        if not label or not asked:
            return None
        key = (label, context, tuple(sorted(c.value for c in asked)))
        if key not in self._cache:
            body = {
                "model": self.model,
                "state": {"element_text": label, "screen_context": context},
                "questions": {c.value: q for c, q in asked.items()},
            }
            resp = await self._client.post(
                URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout
            )
            resp.raise_for_status()
            answers = resp.json()["answers"]
            best = max(
                ((c, float(answers[c.value]["noul"])) for c in asked),
                key=lambda item: item[1],
            )
            self._cache[key] = best
        best = self._cache[key]
        if best and best[1] >= self.threshold:
            return best[0], label, best[1]
        return None
