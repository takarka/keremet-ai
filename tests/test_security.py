import asyncio
import json

import pytest

from security.allowlist import Allowlist, AllowlistViolation
from security.audit import AuditLog
from security.guarded_driver import GuardedDriver
from security.payment_guard import PaymentApprovalRequired, find_payment_label, is_payment_text

TREE = {
    "bounds": "[0,0][1080,2340]",
    "children": [
        {"bounds": "[0,100][1080,300]", "text": "Способ оплаты", "clickable": True},
        {
            "bounds": "[40,2100][1040,2260]",
            "clickable": True,
            "children": [{"bounds": "[300,2150][800,2210]", "text": "Оплатить 2 500 ₸"}],
        },
        {"bounds": "[40,1900][1040,2060]", "contentDescription": "Pay now", "clickable": True},
        {"bounds": "[40,400][1040,560]", "text": "Добавить в корзину", "clickable": True},
    ],
}


class FakeDriver:
    platform = "android"
    supported: set[str] = set()
    supported_buttons: set[str] = set()

    def __init__(self, tree=TREE):
        self.tree = tree
        self.taps = []
        self.started = []

    async def get_ui_tree(self):
        return self.tree

    async def screenshot(self, hide_overlay=True):
        return b"\x89PNG-fake"

    async def tap(self, x, y):
        self.taps.append((x, y))

    async def start_app(self, package, activity=None):
        self.started.append(package)
        return "ok"

    async def press_button(self, button):
        pass


def make_guard(tmp_path, tree=TREE):
    inner = FakeDriver(tree)
    allow = Allowlist({"com.wolt.android"}, {"com.android.settings"})
    return GuardedDriver(inner, allow, AuditLog("t", root=tmp_path)), inner


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("text", ["Оплатить", "оплатить заказ", "Купить билет", "Pay now", "Төлеу", "Перевести"])
def test_payment_text_detected(text):
    assert is_payment_text(text)


@pytest.mark.parametrize("text", ["Способ оплаты", "Payment method", "Добавить в корзину", "", None])
def test_non_payment_text_ignored(text):
    assert not is_payment_text(text)


def test_label_found_via_child_text():
    assert find_payment_label(TREE, 500, 2180) == "Оплатить 2 500 ₸"


def test_label_found_via_content_description():
    assert find_payment_label(TREE, 500, 1980) == "Pay now"


def test_safe_tap_has_no_label():
    assert find_payment_label(TREE, 500, 480) is None
    assert find_payment_label(TREE, 500, 200) is None


def test_payment_tap_blocked_before_device(tmp_path):
    guard, inner = make_guard(tmp_path)
    with pytest.raises(PaymentApprovalRequired) as exc:
        run(guard.tap(500, 2180))
    assert inner.taps == []
    assert exc.value.screenshot == b"\x89PNG-fake"
    assert (tmp_path / "t" / "001-payment-stop.png").exists()


def test_safe_tap_passes(tmp_path):
    guard, inner = make_guard(tmp_path)
    run(guard.tap(500, 480))
    assert inner.taps == [(500, 480)]


def test_everything_blocked_after_stop(tmp_path):
    guard, inner = make_guard(tmp_path)
    with pytest.raises(PaymentApprovalRequired):
        run(guard.tap(500, 2180))
    with pytest.raises(PaymentApprovalRequired):
        run(guard.tap(500, 480))
    with pytest.raises(PaymentApprovalRequired):
        run(guard.press_button("enter"))
    assert inner.taps == []


def test_fail_closed_when_tree_unavailable(tmp_path):
    guard, inner = make_guard(tmp_path)

    async def boom():
        raise RuntimeError("portal down")

    inner.get_ui_tree = boom
    with pytest.raises(PaymentApprovalRequired):
        run(guard.tap(10, 10))
    assert inner.taps == []


def test_allowlist(tmp_path):
    guard, inner = make_guard(tmp_path)
    run(guard.start_app("com.wolt.android"))
    assert inner.started == ["com.wolt.android"]
    for pkg in ("com.android.settings", "com.whatsapp"):
        with pytest.raises(AllowlistViolation):
            run(guard.start_app(pkg))
    assert inner.started == ["com.wolt.android"]


def test_install_uninstall_forbidden(tmp_path):
    guard, _ = make_guard(tmp_path)
    with pytest.raises(PermissionError):
        run(guard.install_app("x.apk"))
    with pytest.raises(PermissionError):
        run(guard.uninstall_app("com.whatsapp"))


def test_audit_log_written_and_no_text_content(tmp_path):
    guard, inner = make_guard(tmp_path)
    inner.input_text = lambda text, clear=False, **kw: asyncio.sleep(0, result=True)
    run(guard.input_text("секретный пароль"))
    lines = (tmp_path / "t" / "actions.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    assert record["event"] == "input_text"
    assert "секретный" not in lines[-1]


def test_default_allowlist_file_loads():
    allow = Allowlist.from_yaml()
    assert "org.telegram.messenger" in allow.allowed
    with pytest.raises(AllowlistViolation):
        allow.check("com.android.settings")


# --- формат Portal: обёртка a11y_tree, boundsInScreen как dict, isClickable ---
PORTAL_TREE = {
    "a11y_tree": {
        "className": "android.widget.FrameLayout",
        "boundsInScreen": {"left": 0, "top": 0, "right": 1080, "bottom": 2400},
        "isClickable": False,
        "children": [
            {
                "className": "android.view.ViewGroup",
                "boundsInScreen": {"left": 40, "top": 2100, "right": 1040, "bottom": 2260},
                "isClickable": True,
                "text": "",
                "children": [
                    {
                        "className": "android.widget.TextView",
                        "boundsInScreen": {"left": 300, "top": 2150, "right": 800, "bottom": 2210},
                        "isClickable": False,
                        "text": "Оплатить",
                    }
                ],
            }
        ],
    },
    "phone_state": {},
    "device_context": {},
}


def test_portal_wrapper_format_detected():
    assert find_payment_label(PORTAL_TREE, 500, 2180) == "Оплатить"
    assert find_payment_label(PORTAL_TREE, 100, 2120) == "Оплатить"  # тап по краю кнопки, текст в потомке
    assert find_payment_label(PORTAL_TREE, 500, 300) is None


def test_portal_payment_tap_blocked(tmp_path):
    guard, inner = make_guard(tmp_path, PORTAL_TREE)
    with pytest.raises(PaymentApprovalRequired):
        run(guard.tap(100, 2120))
    assert inner.taps == []


def test_unrecognized_tree_fails_closed(tmp_path):
    guard, inner = make_guard(tmp_path, {"unexpected": "format"})
    with pytest.raises(PaymentApprovalRequired):
        run(guard.tap(10, 10))
    assert inner.taps == []


def test_repeated_swipes_abort_task(tmp_path):
    from security.guarded_driver import LoopDetected, MAX_REPEATS

    guard, inner = make_guard(tmp_path)

    async def swipe(*a, **k):
        pass

    inner.swipe = swipe
    for _ in range(MAX_REPEATS):
        run(guard.swipe(540, 1600, 540, 600))
    with pytest.raises(LoopDetected):
        run(guard.swipe(540, 1600, 540, 600))
    with pytest.raises(LoopDetected):  # после обрыва заблокировано всё
        run(guard.swipe(1, 2, 3, 4))
    assert guard.stuck is not None


def test_different_actions_do_not_trigger_loop(tmp_path):
    guard, inner = make_guard(tmp_path)

    async def swipe(*a, **k):
        pass

    inner.swipe = swipe
    for i in range(20):
        run(guard.swipe(540, 1600 - i, 540, 600))


@pytest.mark.parametrize("text", ["Оформить", "Оформить покупку", "Подтвердить", "Растау", "Рәсімдеу", "Төлем жасау"])
def test_kaspi_style_final_buttons_blocked(text):
    assert is_payment_text(text)


@pytest.mark.parametrize("text", ["Способ оплаты", "Добавить в корзину", "Выбрать место", "Найти билеты", "Билеттер табу"])
def test_navigation_buttons_not_blocked(text):
    assert not is_payment_text(text)


@pytest.mark.parametrize("text", ["Төлеу", "Төлеу 12 500 ₸", "төлеу 500 тг"])
def test_kk_pay_button_blocked_when_standalone(text):
    assert is_payment_text(text)


@pytest.mark.parametrize("text", ["Бөліп төлеу 0-0-24", "Төлеу әдісі", "Төлеу тарихы"])
def test_kk_pay_words_inside_other_labels_not_blocked(text):
    assert not is_payment_text(text)


def test_empty_screenshot_does_not_crash_halt(tmp_path, monkeypatch):
    guard, inner = make_guard(tmp_path)

    async def empty(hide_overlay=True):
        return b""

    inner.screenshot = empty

    async def no_adb(*a, **k):
        raise FileNotFoundError("adb")

    monkeypatch.setattr("asyncio.create_subprocess_exec", no_adb)
    with pytest.raises(PaymentApprovalRequired) as exc:
        run(guard.tap(500, 2180))
    assert exc.value.screenshot is None
    assert inner.taps == []
    assert not list((tmp_path / "t").glob("*.png"))
