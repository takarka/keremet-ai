import asyncio
import json

import pytest
from cryptography.fernet import Fernet

from security.allowlist import Allowlist
from security.approval import ApprovalSession, Category, InvalidTransition, State
from security.audit import AuditLog
from security.guarded_driver import GuardedDriver
from security.payment_guard import PaymentApprovalRequired, classify_text
from security.secret_store import SecretError, SecretStore

from tests.test_security import FakeDriver


def run(coro):
    return asyncio.run(coro)


# --- Approval Gate: статус-машина -------------------------------------------------

def test_full_approve_flow(tmp_path):
    s = ApprovalSession("t1")
    s.request(Category.FINANCE, "Оплатить", 10, 20, "shot.png")
    assert s.state is State.WAITING_FOR_APPROVAL
    s.approve()
    s.finish()
    assert s.state is State.DONE


def test_cancel_flow():
    s = ApprovalSession("t1")
    s.request(Category.DELETE, "Удалить", 1, 2)
    s.cancel()
    assert s.state is State.CANCELLED


def test_invalid_transitions():
    s = ApprovalSession("t1")
    with pytest.raises(InvalidTransition):
        s.approve()  # нельзя подтвердить то, что не запрашивали
    s.request(Category.FINANCE, "Pay", 1, 2)
    s.cancel()
    with pytest.raises(InvalidTransition):
        s.approve()  # после отмены подтвердить нельзя


def test_timeout_blocks_late_approval():
    s = ApprovalSession("t1", timeout_s=60)
    s.request(Category.FINANCE, "Pay", 1, 2)
    s.requested_at -= 120
    with pytest.raises(InvalidTransition):
        s.approve()
    assert s.state is State.TIMEOUT


def test_session_survives_restart(tmp_path):
    s = ApprovalSession("t1")
    s.request(Category.FINANCE, "Оплатить 2 500 ₸", 540, 2180, "001.png")
    s.save(tmp_path)
    loaded = ApprovalSession.load(tmp_path)
    assert loaded.state is State.WAITING_FOR_APPROVAL
    assert (loaded.label, loaded.x, loaded.y) == ("Оплатить 2 500 ₸", 540, 2180)
    loaded.approve()
    assert loaded.state is State.APPROVED


# --- Классификатор категорий -----------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Оплатить", Category.FINANCE),
        ("Удалить чат", Category.DELETE),
        ("Delete", Category.DELETE),
        ("Отправить", Category.EXTERNAL_SEND),
        ("Send", Category.EXTERNAL_SEND),
        ("Добавить в корзину", None),
        ("", None),
    ],
)
def test_classify(text, expected):
    assert classify_text(text) is expected


def test_classify_respects_policy():
    assert classify_text("Отправить", {Category.FINANCE}) is None


TREE = {
    "a11y_tree": {
        "boundsInScreen": {"left": 0, "top": 0, "right": 1080, "bottom": 2400},
        "children": [
            {"boundsInScreen": {"left": 0, "top": 100, "right": 500, "bottom": 200}, "isClickable": True, "text": "Удалить"},
            {"boundsInScreen": {"left": 500, "top": 100, "right": 1000, "bottom": 200}, "isClickable": True, "text": "Отправить"},
        ],
    }
}


def make(tmp_path, policy=None):
    inner = FakeDriver(TREE)
    kwargs = {"policy": policy} if policy is not None else {}
    return GuardedDriver(inner, Allowlist({"a"}), AuditLog("t", root=tmp_path), **kwargs), inner


def test_delete_blocked_by_default(tmp_path):
    guard, inner = make(tmp_path)
    with pytest.raises(PaymentApprovalRequired) as exc:
        run(guard.tap(100, 150))
    assert exc.value.category is Category.DELETE
    assert (exc.value.x, exc.value.y) == (100, 150)
    assert inner.taps == []


def test_external_send_allowed_by_default_but_blockable(tmp_path):
    guard, inner = make(tmp_path)
    run(guard.tap(700, 150))
    assert inner.taps == [(700, 150)]

    strict, inner2 = make(tmp_path, frozenset({Category.FINANCE, Category.DELETE, Category.EXTERNAL_SEND}))
    with pytest.raises(PaymentApprovalRequired) as exc:
        run(strict.tap(700, 150))
    assert exc.value.category is Category.EXTERNAL_SEND
    assert inner2.taps == []


# --- SecretRefs ------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return SecretStore(tmp_path / "s.enc", key=Fernet.generate_key().decode())


def test_secret_roundtrip_and_encrypted_on_disk(store):
    store.set("passport.number", "N12345678")
    assert store.resolve("№ {{secret:passport.number}}") == "№ N12345678"
    assert b"N12345678" not in store.path.read_bytes()
    assert store.names() == ["passport.number"]


def test_unknown_secret_is_error_not_empty(store):
    with pytest.raises(SecretError):
        store.resolve("{{secret:nope}}")


def test_plain_text_untouched(store):
    assert store.resolve("привет") == "привет"


def test_redact_replaces_values(store):
    store.set("card", "4400111122223333")
    assert store.redact("карта 4400111122223333") == "карта {{secret:card}}"


def test_missing_key_is_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRETS_KEY", raising=False)
    with pytest.raises(SecretError):
        SecretStore(tmp_path / "x.enc")


def test_driver_substitutes_secret_only_at_device_and_never_logs_it(tmp_path, store):
    store.set("passport.number", "N12345678")
    inner = FakeDriver(TREE)
    typed = []

    async def input_text(text, clear=False, **kw):
        typed.append(text)
        return True

    inner.input_text = input_text
    audit = AuditLog("leak", root=tmp_path)
    guard = GuardedDriver(inner, Allowlist({"a"}), audit, secrets=store)
    run(guard.input_text("{{secret:passport.number}}"))

    assert typed == ["N12345678"]  # на устройство ушло настоящее значение
    log = (tmp_path / "leak" / "actions.jsonl").read_text(encoding="utf-8")
    assert "N12345678" not in log  # а в журнале его нет
    assert json.loads(log.splitlines()[-1])["length"] == len("{{secret:passport.number}}")
