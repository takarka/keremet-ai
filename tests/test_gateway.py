import asyncio

import httpx
import pytest

from agent.approval_flow import ApprovalError, execute_approved_tap
from gateway.stt import SttError, transcribe
from security.approval import ApprovalSession, Category, State
from tests.test_phase2_security import TREE
from tests.test_security import FakeDriver


def run(coro):
    return asyncio.run(coro)


# --- STT ------------------------------------------------------------------------

def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_transcribe_ok(monkeypatch):
    monkeypatch.setenv("STT_API_KEY", "k")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"text": "  Открой Kaspi Travel  "})

    text = run(transcribe(b"audio", client=_client(handler)))
    assert text == "Открой Kaspi Travel"
    assert seen["url"].endswith("/audio/transcriptions")
    assert seen["auth"] == "Bearer k"


def test_transcribe_requires_key(monkeypatch):
    monkeypatch.delenv("STT_API_KEY", raising=False)
    with pytest.raises(SttError):
        run(transcribe(b"audio", client=_client(lambda r: httpx.Response(200, json={}))))


@pytest.mark.parametrize("response", [httpx.Response(500, text="boom"), httpx.Response(200, json={"text": ""})])
def test_transcribe_errors(monkeypatch, response):
    monkeypatch.setenv("STT_API_KEY", "k")
    with pytest.raises(SttError):
        run(transcribe(b"audio", client=_client(lambda r: response)))


# --- исполнение подтверждённого действия -----------------------------------------

def make_session(tmp_path, label="Удалить", x=100, y=150):
    s = ApprovalSession("t")
    s.request(Category.DELETE, label, x, y)
    s.save(tmp_path)
    return s


def test_execute_requires_approval(tmp_path):
    make_session(tmp_path)
    with pytest.raises(ApprovalError):
        run(execute_approved_tap(tmp_path, driver=FakeDriver(TREE)))


def test_execute_taps_once_after_approval(tmp_path):
    s = make_session(tmp_path)
    s.approve()
    s.save(tmp_path)
    driver = FakeDriver(TREE)
    msg = run(execute_approved_tap(tmp_path, driver=driver))
    assert driver.taps == [(100, 150)]
    assert "Удалить" in msg
    assert ApprovalSession.load(tmp_path).state is State.DONE
    with pytest.raises(ApprovalError):  # повторно нажать нельзя
        run(execute_approved_tap(tmp_path, driver=driver))
    assert driver.taps == [(100, 150)]


def test_execute_aborts_if_screen_changed(tmp_path):
    s = make_session(tmp_path, label="Оплатить")  # на экране теперь «Удалить», а не «Оплатить»
    s.approve()
    s.save(tmp_path)
    driver = FakeDriver(TREE)
    with pytest.raises(ApprovalError):
        run(execute_approved_tap(tmp_path, driver=driver))
    assert driver.taps == []


# --- бот ----------------------------------------------------------------------------

def test_bot_refuses_without_config(monkeypatch):
    from gateway.telegram_bot import build_app

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    with pytest.raises(SystemExit):
        build_app()


def test_bot_builds_and_parses_whitelist(monkeypatch):
    from gateway.telegram_bot import allowed_ids, build_app

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111, 222")
    assert allowed_ids() == {111, 222}
    assert build_app() is not None
