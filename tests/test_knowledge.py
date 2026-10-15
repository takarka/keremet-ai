import asyncio
import json

from agent.knowledge import LESSONS_HEADER, add_lesson, list_cards
from agent.runner import KNOWLEDGE_DIR


def run(coro):
    return asyncio.run(coro)


def test_add_lesson_creates_card_and_mapping(tmp_path):
    card = add_lesson("com.example.app", "Кнопка «Далее» внизу справа", tmp_path)
    assert card.name == "com.example.app.md"
    assert json.loads((tmp_path / "app_cards.json").read_text(encoding="utf-8")) == {"com.example.app": "com.example.app.md"}
    text = card.read_text(encoding="utf-8")
    assert LESSONS_HEADER in text and "Кнопка «Далее»" in text


def test_add_lesson_appends_to_existing_card(tmp_path):
    add_lesson("com.example.app", "первое", tmp_path)
    add_lesson("com.example.app", "второе", tmp_path)
    text = (tmp_path / "com.example.app.md").read_text(encoding="utf-8")
    assert text.count(LESSONS_HEADER) == 1
    assert "первое" in text and "второе" in text
    assert list(json.loads((tmp_path / "app_cards.json").read_text(encoding="utf-8"))) == ["com.example.app"]


def test_add_lesson_rejects_empty(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        add_lesson("com.example.app", "   ", tmp_path)


def test_list_cards_counts_lines(tmp_path):
    add_lesson("a.b", "x", tmp_path)
    assert list_cards(tmp_path)["a.b"] >= 3


def test_mobilerun_loads_our_kaspi_card():
    """Настоящий загрузчик mobilerun должен подхватывать карточки из нашей папки знаний."""
    from mobilerun.app_cards.providers import LocalAppCardProvider

    provider = LocalAppCardProvider(app_cards_dir=str(KNOWLEDGE_DIR))
    card = run(provider.load_app_card("kz.kaspi.mobile", platform="android"))
    assert "Таңдау" in card and "Билеттер табу" in card
    assert run(provider.load_app_card("ru.dublgis.dgismobile", platform="android"))
    assert run(provider.load_app_card("unknown.pkg", platform="android")) == ""


# --- подмешивание в задачу ---------------------------------------------------------

def test_task_mentioning_kaspi_gets_the_card():
    from agent.knowledge import with_knowledge

    goal, used = with_knowledge("Открой Kaspi и найди поезда в Шымкент")
    assert used == ["kz.kaspi.mobile"]
    assert goal.startswith("Открой Kaspi и найди поезда в Шымкент")
    assert "Таңдау" in goal and "Билеттер табу" in goal


def test_russian_alias_and_multiple_apps():
    from agent.knowledge import with_knowledge

    _, used = with_knowledge("Найди в 2ГИС кафе, потом проверь Каспи")
    assert set(used) == {"ru.dublgis.dgismobile", "kz.kaspi.mobile"}


def test_unrelated_task_unchanged():
    from agent.knowledge import with_knowledge

    goal, used = with_knowledge("Открой Telegram и вернись домой")
    assert goal == "Открой Telegram и вернись домой" and used == []


def test_new_package_gets_alias_and_is_found(tmp_path):
    from agent.knowledge import cards_for_task

    add_lesson("com.wolt.android", "Корзина — значок справа сверху", tmp_path)
    aliases = tmp_path / "aliases.json"
    assert json.loads(aliases.read_text(encoding="utf-8")) == {"com.wolt.android": ["android"]}
    # алиас «android» слишком общий — пользователь дополняет его вручную; проверяем сам механизм поиска
    aliases.write_text(json.dumps({"com.wolt.android": ["wolt"]}), encoding="utf-8")
    text, used = cards_for_task("Закажи в Wolt кофе", tmp_path, aliases)
    assert used == ["com.wolt.android"] and "Корзина" in text
