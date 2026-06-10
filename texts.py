import json
import os

_BASE = os.path.dirname(os.path.abspath(__file__))


def _load(bot_type: str) -> dict:
    result = {}
    for lang in ("en", "ru", "tr"):
        path = os.path.join(_BASE, "locales", bot_type, f"{lang}.json")
        with open(path, encoding="utf-8") as f:
            result[lang] = json.load(f)
    return result


CLIENT_TEXTS: dict = _load("client")
DRIVER_TEXTS: dict = _load("driver")
