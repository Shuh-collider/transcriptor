"""Библиотека готовых расшифровок: список видео и поиск по всем текстам."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from . import config

_cache: dict[Path, tuple[int, dict]] = {}
_lock = threading.Lock()


def norm(text: str | None) -> str:
    return str(text or "").lower().replace("ё", "е")


def _load(path: Path) -> dict | None:
    """transcript.json с кэшем по времени изменения файла."""
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    with _lock:
        _cache[path] = (mtime, doc)
    return doc


def all_docs() -> list[dict]:
    docs = [doc for path in config.VIDEOS_DIR.glob("*/transcript.json") if (doc := _load(path))]
    docs.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    return docs


def speaker_name(doc: dict, label: str | None) -> str:
    if not label:
        return ""
    return ((doc.get("speakers") or {}).get(label) or {}).get("name") or f"Спикер {label.lstrip('S')}"


def watch_url(url: str | None, seconds: float) -> str | None:
    if not url:
        return None
    return f"{url}{'&' if '?' in url else '?'}t={int(seconds)}s"


def video_card(doc: dict) -> dict:
    video = doc.get("video") or {}
    summary = doc.get("summary") or {}
    return {
        "video_id": video.get("id"),
        "title": video.get("title"),
        "channel": video.get("channel"),
        "url": video.get("url"),
        "duration": video.get("duration"),
        "upload_date": video.get("upload_date"),
        "view_count": video.get("view_count"),
        "like_count": video.get("like_count"),
        "stats_at": video.get("stats_at"),
        "language": doc.get("language"),
        "engine": doc.get("engine"),
        "speakers": {label: speaker_name(doc, label) for label in doc.get("speakers") or {}},
        "segments": len(doc.get("segments") or []),
        "has_summary": bool(summary),
        "tldr": summary.get("tldr"),
        "created_at": doc.get("created_at"),
    }


def _page(items: list, limit: int, offset: int) -> dict:
    page = items[offset : offset + limit]
    more = offset + len(page) < len(items)
    return {
        "total": len(items),
        "count": len(page),
        "offset": offset,
        "has_more": more,
        "next_offset": offset + len(page) if more else None,
        "items": page,
    }


def list_videos(query: str = "", limit: int = 20, offset: int = 0) -> dict:
    cards = [video_card(doc) for doc in all_docs()]
    words = norm(query).split()
    if words:
        cards = [
            c for c in cards
            if all(w in norm(f"{c['title']} {c['channel']} {c['tldr'] or ''} {' '.join(c['speakers'].values())}") for w in words)
        ]
    return _page(cards, limit, offset)


def _snippet(text: str, pos: int, width: int = 160) -> str:
    start = max(0, pos - width // 2)
    end = min(len(text), start + width)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def search(query: str, video_id: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    """Реплики, в которых встречаются все слова запроса (без учёта регистра и ё/е)."""
    words = norm(query).split()
    hits: list[dict] = []
    if words:
        for doc in all_docs():
            video = doc.get("video") or {}
            if video_id and video.get("id") != video_id:
                continue
            for seg in doc.get("segments") or []:
                low = norm(seg["text"])
                if not all(w in low for w in words):
                    continue
                hits.append(
                    {
                        "video_id": video.get("id"),
                        "title": video.get("title"),
                        "start": seg["start"],
                        "speaker": seg.get("speaker"),
                        "speaker_name": speaker_name(doc, seg.get("speaker")),
                        "snippet": _snippet(seg["text"], low.find(words[0])),
                        "url": watch_url(video.get("url"), seg["start"]),
                    }
                )
    return _page(hits, limit, offset)
