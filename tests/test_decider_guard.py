import asyncio
import json

import pytest

from security.allowlist import Allowlist
from security.approval import Category
from security.audit import AuditLog
from security.decider_guard import DeciderGuard
from security.guarded_driver import GuardedDriver
from security.payment_guard import PaymentApprovalRequired, screen_texts, texts_at
from tests.test_security import TREE, FakeDriver


def run(coro):
    return asyncio.run(coro)


class FakeResponse:
    def __init__(self, answers, status=200):
        self._answers, self.status_code = answers, status

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"answers": self._answers}


class FakeClient:
    def __init__(self, probs=None, fail=False):
        self.probs, self.fail, self.calls = probs or {}, fail, []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(json)
        if self.fail:
            raise TimeoutError("api down")
        return FakeResponse({q: {"type": "noul", "noul": self.probs.get(q, 0.01)} for q in json["questions"]})


def guard_with(tmp_path, client, tree=TREE):
    inner = FakeDriver(tree)
    decider = DeciderGuard("k", client=client)
    g = GuardedDriver(inner, Allowlist({"a"}), AuditLog("t", root=tmp_path), decider=decider)
    return g, inner


def events(tmp_path):
    return [json.loads(l)["event"] for l in (tmp_path / "t" / "actions.jsonl").read_text(encoding="utf-8").splitlines()]


def test_texts_at_returns_button_texts():
    assert texts_at(TREE, 500, 2180) == ["Оплатить 2 500 ₸"]
    assert "Добавить в корзину" in screen_texts(TREE)


def test_decider_adds_stop_when_regex_misses(tmp_path):
    """Кнопка без слов из regex, но модель уверена, что это платёж, — тап не выполняется."""
    tree = {"bounds": "[0,0][1080,2340]", "children": [
        {"bounds": "[40,400][1040,560]", "text": "Жалғастыру 4 200 ₸", "clickable": True}]}
    g, inner = guard_with(tmp_path, FakeClient({"finance": 0.93}), tree)
    with pytest.raises(PaymentApprovalRequired) as e:
        run(g.tap(500, 480))
    assert e.value.category is Category.FINANCE
    assert inner.taps == [] and "decider_stop" in events(tmp_path)


def test_decider_low_probability_allows_tap(tmp_path):
    g, inner = guard_with(tmp_path, FakeClient({"finance": 0.05}))
    run(g.tap(500, 480))
    assert inner.taps == [(500, 480)]


def test_decider_failure_does_not_block_or_allow_by_itself(tmp_path):
    """Сбой API: решает regex — безопасная кнопка проходит, платёжная всё равно остановлена."""
    g, inner = guard_with(tmp_path, FakeClient(fail=True))
    run(g.tap(500, 480))
    assert inner.taps == [(500, 480)] and "decider_error" in events(tmp_path)
    with pytest.raises(PaymentApprovalRequired):
        run(g.tap(500, 2180))


def test_regex_stop_does_not_call_decider(tmp_path):
    client = FakeClient()
    g, _ = guard_with(tmp_path, client)
    with pytest.raises(PaymentApprovalRequired):
        run(g.tap(500, 2180))
    assert client.calls == []


def test_decider_asks_only_policy_categories(tmp_path):
    client = FakeClient()
    g, _ = guard_with(tmp_path, client)
    run(g.tap(500, 480))
    assert set(client.calls[0]["questions"]) == {"finance", "delete"}


def test_decider_caches_same_label(tmp_path):
    client = FakeClient()
    g, _ = guard_with(tmp_path, client)
    run(g.tap(500, 480))
    run(g.tap(510, 490))
    assert len(client.calls) == 1
