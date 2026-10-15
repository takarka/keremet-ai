import asyncio
import json
import time

import pytest

from agent import pricing
from agent.config import Settings
from security.allowlist import Allowlist
from security.audit import AuditLog
from security.guarded_driver import (
    MAX_SAME_TAP,
    MAX_SAME_TEXT,
    BudgetExceeded,
    GuardedDriver,
    LoopDetected,
)
from tests.test_security import FakeDriver

SAFE_TREE = {
    "a11y_tree": {
        "boundsInScreen": {"left": 0, "top": 0, "right": 1080, "bottom": 2400},
        "text": "",
    }
}


def run(coro):
    return asyncio.run(coro)


def make(tmp_path):
    inner = FakeDriver(SAFE_TREE)

    async def input_text(text, clear=False, **kw):
        return True

    inner.input_text = input_text
    return GuardedDriver(inner, Allowlist({"a"}), AuditLog("t", root=tmp_path)), inner


def test_same_tap_point_repeated_across_task_is_loop(tmp_path):
    """Цикл «поиск → ввод → назад» с другими действиями между тапами в одну и ту же точку."""
    guard, inner = make(tmp_path)
    for i in range(MAX_SAME_TAP):
        run(guard.tap(468, 179))
        run(guard.tap(100 + i * 100, 1500))  # между ними — другие тапы, «подряд одинаковых» нет
    with pytest.raises(LoopDetected):
        run(guard.tap(470, 181))  # та же область (сетка 40 px)
    assert guard.stuck is not None


def test_distinct_taps_are_fine(tmp_path):
    guard, inner = make(tmp_path)
    for i in range(30):
        run(guard.tap(30 + (i % 25) * 40, 100 + (i // 25) * 80))
    assert guard.stuck is None


def test_same_text_typed_too_often_is_loop(tmp_path):
    guard, _ = make(tmp_path)
    for _ in range(MAX_SAME_TEXT):
        run(guard.input_text("USB кабель", clear=True))
    with pytest.raises(LoopDetected):
        run(guard.input_text("USB кабель", clear=True))


def test_different_texts_are_fine(tmp_path):
    guard, _ = make(tmp_path)
    for i in range(10):
        run(guard.input_text(f"запрос {i}"))
    assert guard.stuck is None


def test_typed_text_never_logged_by_loop_tracking(tmp_path):
    guard, _ = make(tmp_path)
    run(guard.input_text("секретный запрос"))
    assert "секретный" not in (tmp_path / "t" / "actions.jsonl").read_text(encoding="utf-8")


def test_app_restarted_too_often_is_loop(tmp_path):
    from security.guarded_driver import MAX_APP_STARTS

    guard, inner = make(tmp_path)
    guard.allowlist = Allowlist({"a"})
    for _ in range(MAX_APP_STARTS):
        run(guard.start_app("a"))
    with pytest.raises(LoopDetected):
        run(guard.start_app("a"))
    assert len(inner.started) == MAX_APP_STARTS


def test_abort_blocks_every_later_action(tmp_path):
    guard, inner = make(tmp_path)
    guard.abort(BudgetExceeded("лимит"))
    for call in (guard.tap(1, 1), guard.swipe(1, 2, 3, 4), guard.press_button("back"), guard.start_app("a")):
        with pytest.raises(BudgetExceeded):
            run(call)
    assert inner.taps == [] and inner.started == []
    events = [json.loads(line)["event"] for line in (tmp_path / "t" / "actions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "task_aborted" in events


# --- цены -----------------------------------------------------------------------

def settings(**kw):
    base = dict(api_key="k", base_url="https://openrouter.ai/api/v1", model="vendor/model", serial=None,
                price_in=3.0, price_out=15.0, max_cost_usd=0.25, max_seconds=300)
    base.update(kw)
    return Settings(**base)


def test_price_from_cache(tmp_path, monkeypatch):
    cache = tmp_path / "pricing.json"
    cache.write_text(json.dumps({"fetched_at": time.time(), "prices": {"vendor/model": [0.3, 2.5]}}), encoding="utf-8")
    monkeypatch.setattr(pricing, "CACHE", cache)
    assert pricing.price_per_million(settings()) == (0.3, 2.5, "openrouter")


def test_price_falls_back_to_env_when_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(pricing, "CACHE", tmp_path / "none.json")

    def offline(_):
        raise OSError("нет сети")

    monkeypatch.setattr(pricing, "_fetch", offline)
    assert pricing.price_per_million(settings()) == (3.0, 15.0, "env")


def test_non_openrouter_provider_uses_env(tmp_path, monkeypatch):
    monkeypatch.setattr(pricing, "CACHE", tmp_path / "none.json")
    assert pricing.price_per_million(settings(base_url="https://api.example.com/v1")) == (3.0, 15.0, "env")


def test_cost_formula():
    from agent.runner import cost_usd

    assert cost_usd(1_000_000, 1_000_000, 0.3, 2.5) == pytest.approx(2.8)


def test_timeout_abort_blocks_actions(tmp_path):
    from security.guarded_driver import TaskTimeout

    guard, inner = make(tmp_path)
    guard.abort(TaskTimeout("время"))
    with pytest.raises(TaskTimeout):
        run(guard.tap(1, 1))
    assert inner.taps == []


# --- оживление Portal ---------------------------------------------------------------

def _revive_setup(tmp_path, monkeypatch, fail_times):
    guard, inner = make(tmp_path)
    calls = {"n": 0}

    async def flaky_tree():
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise RuntimeError("Portal returned error: Accessibility service not available")
        return SAFE_TREE

    inner.get_ui_tree = flaky_tree
    adb_calls = []

    async def fake_adb(*args):
        adb_calls.append(args)
        return "MobilerunAccessibilityService" if args[:2] == ("shell", "dumpsys") else ""

    async def no_sleep(_):
        return None

    monkeypatch.setattr(guard, "_adb", fake_adb)
    monkeypatch.setattr("security.guarded_driver.asyncio.sleep", no_sleep)
    return guard, inner, adb_calls


def test_dead_accessibility_is_revived_and_retried(tmp_path, monkeypatch):
    guard, inner, adb_calls = _revive_setup(tmp_path, monkeypatch, fail_times=1)
    run(guard.tap(500, 500))
    assert inner.taps == [(500, 500)]
    assert any("force-stop" in a for a in adb_calls)
    assert any(a[:2] == ("shell", "settings") and a[-1] == "1" for a in adb_calls)


def test_revive_limited_per_task(tmp_path, monkeypatch):
    from security.guarded_driver import MAX_REVIVES, PaymentApprovalRequired

    guard, inner, adb_calls = _revive_setup(tmp_path, monkeypatch, fail_times=10_000)
    for _ in range(MAX_REVIVES + 2):
        with pytest.raises(PaymentApprovalRequired):  # fail-closed: без дерева экрана тап не выполняется
            run(guard.tap(1, 1))
    assert sum(1 for a in adb_calls if "force-stop" in a) == MAX_REVIVES
    assert inner.taps == []


def test_unrelated_errors_not_revived(tmp_path, monkeypatch):
    guard, inner, adb_calls = _revive_setup(tmp_path, monkeypatch, fail_times=0)

    async def boom():
        raise RuntimeError("что-то другое")

    inner.get_ui_tree = boom
    with pytest.raises(RuntimeError):
        run(guard.get_ui_tree())
    assert adb_calls == []
