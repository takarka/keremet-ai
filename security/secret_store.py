"""SecretRefs: LLM видит только плейсхолдеры {{secret:name}}, значение подставляется локально (P2-11).

Хранилище шифруется Fernet; ключ берётся из SECRETS_KEY (.env), в репозиторий не попадает.
Создать ключ:  python -m security.secret_store init
Добавить:      python -m security.secret_store set passport.number     (значение вводится скрыто)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "data" / "secrets.enc"
_REF_RE = re.compile(r"\{\{secret:([A-Za-z0-9_.\-]+)\}\}")


class SecretError(Exception):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode()


class SecretStore:
    def __init__(self, path: Path | str = DEFAULT_PATH, key: str | None = None) -> None:
        key = key or os.environ.get("SECRETS_KEY", "")
        if not key:
            raise SecretError("SECRETS_KEY не задан: выполните `python -m security.secret_store init` и добавьте ключ в .env")
        self._fernet = Fernet(key.encode())
        self.path = Path(path)

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        raw = self._fernet.decrypt(self.path.read_bytes())
        return json.loads(raw.decode("utf-8"))

    def _save(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(self._fernet.encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8")))

    def set(self, name: str, value: str) -> None:
        data = self._load()
        data[name] = value
        self._save(data)

    def names(self) -> list[str]:
        return sorted(self._load())

    def resolve(self, text: str) -> str:
        """Подставить значения вместо {{secret:name}}. Неизвестный секрет — ошибка, а не пустая строка."""
        if "{{secret:" not in text:
            return text
        data = self._load()

        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in data:
                raise SecretError(f"Секрет «{name}» не найден")
            return data[name]

        return _REF_RE.sub(sub, text)

    def redact(self, text: str) -> str:
        """Заменить любые известные значения секретов на плейсхолдеры (для логов и промптов)."""
        for name, value in self._load().items():
            if value:
                text = text.replace(value, "{{secret:%s}}" % name)
        return text


def main(argv: list[str] | None = None) -> int:
    import getpass
    import sys

    args = argv if argv is not None else sys.argv[1:]
    if args[:1] == ["init"]:
        print("SECRETS_KEY=" + generate_key())
        print("Добавьте строку выше в .env (файл в git не попадает).")
        return 0
    if args[:1] == ["set"] and len(args) == 2:
        SecretStore().set(args[1], getpass.getpass(f"Значение для {args[1]}: "))
        print("Сохранено.")
        return 0
    if args[:1] == ["list"]:
        from agent.config import load_settings  # noqa: F401  (подгружает .env)

        print("\n".join(SecretStore().names()) or "(пусто)")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
