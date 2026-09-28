"""Пути, ключи и настройки проекта."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def reload_env() -> None:
    """Перечитывает .env, чтобы новые ключи подхватывались без перезапуска сервера."""
    load_dotenv(ENV_FILE, override=True)


reload_env()

DATA_DIR = Path(os.getenv("TRANSCRIPTOR_DATA_DIR") or ROOT / "data")
VIDEOS_DIR = DATA_DIR / "videos"
DB_PATH = DATA_DIR / "transcriptor.db"

HOST = os.getenv("TRANSCRIPTOR_HOST", "127.0.0.1")
PORT = int(os.getenv("TRANSCRIPTOR_PORT", "8000"))

# Модели для пересказа. Цены за 1 млн токенов (вход / выход) — чтобы показывать стоимость;
# у Gemini цену не считаем: ключ из AI Studio обычно работает на бесплатном тарифе.
SUMMARY_MODELS: dict[str, dict] = {
    "gemini-3.8-flash": {"title": "Gemini 3.8 Flash", "provider": "gemini"},
    "gemini-3.1-pro-preview": {"title": "Gemini 3.1 Pro (только платно)", "provider": "gemini"},
    "gemini-3.5-flash-lite": {"title": "Gemini 3.5 Flash-Lite", "provider": "gemini"},
    "claude-opus-5": {"title": "Claude Opus 5", "provider": "anthropic", "input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"title": "Claude Sonnet 5", "provider": "anthropic", "input": 2.0, "output": 10.0},
    "claude-haiku-4-5": {"title": "Claude Haiku 4.5", "provider": "anthropic", "input": 1.0, "output": 5.0},
}
DEFAULT_MODEL_BY_PROVIDER = {"anthropic": "claude-opus-5", "gemini": "gemini-3.8-flash"}

# Выгружать модели из видеопамяти, если очередь простаивает столько секунд
IDLE_UNLOAD_SECONDS = int(os.getenv("TRANSCRIPTOR_IDLE_UNLOAD", "600"))


def hf_token() -> str | None:
    return os.getenv("HF_TOKEN") or None


def anthropic_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def gemini_configured() -> bool:
    return bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))


def providers() -> dict[str, bool]:
    """Для каких сервисов пересказа в .env есть ключ."""
    return {"anthropic": anthropic_configured(), "gemini": gemini_configured()}


def default_summary_model() -> str:
    """SUMMARY_MODEL из .env, если для неё есть ключ, иначе модель того сервиса, чей ключ есть."""
    available = providers()
    wanted = os.getenv("SUMMARY_MODEL")
    if wanted in SUMMARY_MODELS and available[SUMMARY_MODELS[wanted]["provider"]]:
        return wanted
    for provider in ("anthropic", "gemini"):
        if available[provider]:
            return DEFAULT_MODEL_BY_PROVIDER[provider]
    return wanted if wanted in SUMMARY_MODELS else DEFAULT_MODEL_BY_PROVIDER["gemini"]


def resolve_summary_model(model: str | None) -> str:
    """Выбранная модель, а если для её сервиса нет ключа — модель того сервиса, для которого он есть."""
    if model in SUMMARY_MODELS and providers()[SUMMARY_MODELS[model]["provider"]]:
        return model
    return default_summary_model()


def cookies_from_browser() -> str | None:
    """Браузер, из которого yt-dlp возьмёт cookies, если YouTube просит подтвердить, что вы не бот."""
    return os.getenv("YTDLP_COOKIES_FROM_BROWSER") or None


def prepare_runtime() -> None:
    """Готовит процесс к работе: сертификаты, PATH для Deno, UTF-8 в консоли, папка данных."""
    import truststore

    # httpx (Hugging Face, Anthropic SDK) по умолчанию не видит сертификаты Windows, и HTTPS
    # ломается, если антивирус или корпоративный прокси проверяет трафик своим сертификатом
    truststore.inject_into_ssl()

    scripts = Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin")
    path = os.environ.get("PATH", "")
    if str(scripts) not in path.split(os.pathsep):
        os.environ["PATH"] = str(scripts) + os.pathsep + path
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
