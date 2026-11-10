"""Language of messages: English or Russian.

The language is chosen in the web interface and kept in data/settings.json, so job progress,
errors and exported files follow it. Without a saved choice: TRANSCRIPTOR_LANG, otherwise English.
"""

from __future__ import annotations

import json
import os
import threading

from . import config

LANGS = ("en", "ru")
_SETTINGS = config.DATA_DIR / "settings.json"
_lock = threading.Lock()
_lang: str | None = None


def language() -> str:
    global _lang
    if _lang is None:
        try:
            saved = json.loads(_SETTINGS.read_text(encoding="utf-8")).get("language")
        except (OSError, ValueError, AttributeError):
            saved = None
        env = (os.getenv("TRANSCRIPTOR_LANG") or "").strip().lower()[:2]
        _lang = saved if saved in LANGS else env if env in LANGS else "en"
    return _lang


def is_saved() -> bool:
    """Выбирал ли пользователь язык в интерфейсе (иначе интерфейс возьмёт язык браузера)."""
    try:
        return json.loads(_SETTINGS.read_text(encoding="utf-8")).get("language") in LANGS
    except (OSError, ValueError, AttributeError):
        return False


def set_language(lang: str) -> str:
    global _lang
    if lang not in LANGS:
        raise ValueError(lang)
    with _lock:
        try:
            settings = json.loads(_SETTINGS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        settings["language"] = lang
        _SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        _SETTINGS.write_text(json.dumps(settings, ensure_ascii=False, indent=1), encoding="utf-8")
        _lang = lang
    return lang


def L(en: str, ru: str) -> str:
    """The English or Russian text, depending on the chosen language."""
    return ru if language() == "ru" else en


def plural(n: int, en: tuple[str, str], ru: tuple[str, str, str]) -> str:
    """Word form for a number: plural(5, ("view", "views"), ("просмотр", "просмотра", "просмотров"))."""
    if language() != "ru":
        return en[0] if n == 1 else en[1]
    n = abs(n) % 100
    if 11 <= n <= 19:
        return ru[2]
    n %= 10
    return ru[0] if n == 1 else ru[1] if 2 <= n <= 4 else ru[2]
