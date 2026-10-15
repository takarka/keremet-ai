"""Payment Guard: безусловная остановка перед платёжными кнопками (P1-11).

Решение принимается по тексту / content-description элемента под точкой тапа,
до того как тап уйдёт на устройство.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterator

from security.approval import Category

# Границы слов нужны, чтобы «Способ оплаты» или «Payment method» не срабатывали.
_PAYMENT_PATTERNS = [
    r"оплатить", r"купить", r"перевести", r"заказать", r"оформить заказ",
    r"подтвердить заказ", r"подтвердить оплату", r"отправить перевод",
    # Финальные кнопки Kaspi и аналогов: «Оформить», «Подтвердить» — блокируем с запасом (fail-safe).
    r"оформить", r"подтвердить", r"растау", r"рәсімдеу", r"төлем жасау",
    r"pay", r"pay now", r"buy", r"buy now", r"purchase", r"place order",
    r"confirm order", r"confirm payment", r"checkout",
    r"сатып алу", r"аудару", r"тапсырыс беру",
]
PAYMENT_RE = re.compile(r"\b(?:" + "|".join(_PAYMENT_PATTERNS) + r")\b", re.IGNORECASE)
# «Төлеу» (оплатить) входит во множество безопасных названий («Бөліп төлеу 0-0-24»), поэтому
# блокируем его только как отдельную кнопку: «Төлеу» или «Төлеу 12 500 ₸».
PAYMENT_EXACT_RE = re.compile(r"төлеу(?:\s+[\d\s.,]+\s*(?:₸|тг|тенге)?)?", re.IGNORECASE)

_TEXT_KEYS = ("text", "contentDescription", "content-desc", "desc", "label", "hint")
_CHILD_KEYS = ("children", "child", "nodes")
_CLICKABLE_KEYS = ("isClickable", "clickable")
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


_DELETE_RE = re.compile(r"\b(?:удалить|удалить все|очистить|delete|remove|clear all|өшіру|жою)\b", re.IGNORECASE)
_SEND_RE = re.compile(r"\b(?:отправить|send|share|поделиться|жіберу)\b", re.IGNORECASE)


class PaymentApprovalRequired(Exception):
    """Тап по рискованной кнопке заблокирован; нужно подтверждение человека."""

    def __init__(
        self,
        label: str,
        screenshot: bytes | None = None,
        category: Category = Category.FINANCE,
        x: int | None = None,
        y: int | None = None,
    ) -> None:
        super().__init__(f"Остановка ({category.value}): «{label}». Требуется подтверждение пользователя.")
        self.label = label
        self.screenshot = screenshot
        self.category = category
        self.x = x
        self.y = y


def is_payment_text(text: str | None) -> bool:
    if not text:
        return False
    return PAYMENT_RE.search(text) is not None or PAYMENT_EXACT_RE.fullmatch(text.strip()) is not None


def classify_text(text: str | None, categories: frozenset[Category] | set[Category] | None = None) -> Category | None:
    """Категория риска для текста кнопки; финансы проверяются первыми."""
    if not text:
        return None
    allowed = categories if categories is not None else {Category.FINANCE, Category.DELETE, Category.EXTERNAL_SEND}
    if Category.FINANCE in allowed and is_payment_text(text):
        return Category.FINANCE
    if Category.DELETE in allowed and _DELETE_RE.search(text):
        return Category.DELETE
    if Category.EXTERNAL_SEND in allowed and _SEND_RE.search(text):
        return Category.EXTERNAL_SEND
    return None


def _bounds(node: dict[str, Any]) -> tuple[int, int, int, int] | None:
    raw = node.get("bounds") or node.get("boundsInScreen") or node.get("rect")
    if isinstance(raw, str):
        m = _BOUNDS_RE.search(raw)
        return tuple(int(v) for v in m.groups()) if m else None  # type: ignore[return-value]
    if isinstance(raw, dict):
        try:
            return (int(raw["left"]), int(raw["top"]), int(raw["right"]), int(raw["bottom"]))
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            return tuple(int(v) for v in raw)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None
    return None


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    for key in _CHILD_KEYS:
        value = node.get(key)
        if isinstance(value, list):
            return [c for c in value if isinstance(c, dict)]
    return []


def _walk(node: dict[str, Any], path: tuple[dict[str, Any], ...] = ()) -> Iterator[tuple[dict[str, Any], tuple]]:
    here = path + (node,)
    yield node, here
    for child in _children(node):
        yield from _walk(child, here)


def _own_texts(node: dict[str, Any]) -> Iterator[str]:
    for key in _TEXT_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            yield value


def _texts(node: dict[str, Any]) -> Iterator[str]:
    """Все тексты узла и его потомков (текст кнопки часто лежит в дочернем TextView)."""
    for key in _TEXT_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            yield value
    for child in _children(node):
        yield from _texts(child)


def find_risky(tree: Any, x: int, y: int, categories: frozenset[Category] | set[Category]) -> tuple[Category, str] | None:
    """(категория, текст) рискованной кнопки под точкой (x, y) или None."""
    return _scan(tree, x, y, lambda t: classify_text(t, categories))


def find_payment_label(tree: Any, x: int, y: int) -> str | None:
    """Вернуть текст платёжной кнопки под точкой (x, y) или None."""
    hit = _scan(tree, x, y, lambda t: Category.FINANCE if is_payment_text(t) else None)
    return hit[1] if hit else None


def _scan(tree: Any, x: int, y: int, matcher: Callable[[str], Category | None]) -> tuple[Category, str] | None:
    """Общий обход дерева.

    Проверяются самый маленький элемент под точкой и ближайший кликабельный
    предок вместе со всеми потомками.
    """
    # Portal возвращает обёртку {"a11y_tree": {...}, "phone_state": ..., ...}.
    if isinstance(tree, dict) and "a11y_tree" in tree:
        tree = tree["a11y_tree"]
    roots = tree if isinstance(tree, list) else [tree]
    hits: list[tuple[int, tuple]] = []
    seen_bounds = False
    for root in roots:
        if not isinstance(root, dict):
            continue
        for node, path in _walk(root):
            b = _bounds(node)
            if b is None:
                continue
            seen_bounds = True
            if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                hits.append(((b[2] - b[0]) * (b[3] - b[1]), path))
    if not seen_bounds:
        # Формат дерева не распознан: молча пропускать нельзя (fail-closed в GuardedDriver).
        raise ValueError("UI-дерево не содержит элементов с координатами")
    if not hits:
        return None

    _, path = min(hits, key=lambda h: h[0])
    innermost = path[-1]
    candidates = [_own_texts(innermost)]
    # Текст кнопки часто в дочернем TextView, поэтому у ближайшего кликабельного
    # элемента проверяем всё поддерево; у некликабельного фона — только его собственный текст.
    for node in reversed(path):
        if any(node.get(k) in (True, "true", "True") for k in _CLICKABLE_KEYS):
            candidates.append(_texts(node))
            break
    for texts in candidates:
        for text in texts:
            category = matcher(text)
            if category is not None:
                return category, text.strip()
    return None
