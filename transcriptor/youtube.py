"""YouTube через yt-dlp: раскрытие плейлистов и каналов, загрузка звуковой дорожки."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import yt_dlp

from . import config
from .media import ffmpeg_exe

log = logging.getLogger(__name__)

_CHANNEL_ROOT = re.compile(
    r"^https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+)/?$", re.I
)


class DownloadError(RuntimeError):
    pass


@dataclass
class VideoRef:
    id: str
    url: str
    title: str
    duration: float | None = None
    channel: str | None = None
    thumbnail: str | None = None
    view_count: int | None = None  # в списках канала YouTube отдаёт округлённое число

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Expanded:
    kind: str  # video | playlist | channel
    title: str
    entries: list[VideoRef]


class _Logger:
    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)


def _base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _Logger(),
        "ffmpeg_location": ffmpeg_exe(),
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
    }
    browser = config.cookies_from_browser()
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def _thumbnail(info: dict) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = info.get("thumbnails") or []
    return thumbs[-1].get("url") if thumbs else (f"https://i.ytimg.com/vi/{info['id']}/mqdefault.jpg" if info.get("id") else None)


def _ref(entry: dict) -> VideoRef | None:
    vid = entry.get("id")
    title = entry.get("title") or ""
    if not vid or title in ("[Private video]", "[Deleted video]"):
        return None
    return VideoRef(
        id=vid,
        url=entry.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
        title=title or vid,
        duration=entry.get("duration"),
        channel=entry.get("channel") or entry.get("uploader"),
        thumbnail=f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        view_count=entry.get("view_count"),
    )


def _is_single_video(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.endswith("youtu.be"):
        return True
    return parsed.path.startswith(("/watch", "/shorts/", "/live/")) and (
        "v" in parse_qs(parsed.query) or not parsed.path.startswith("/watch")
    )


def friendly_error(exc: Exception) -> str:
    text = str(exc)
    low = text.lower()
    if "confirm you" in low and "bot" in low:
        return (
            "YouTube просит подтвердить, что вы не бот. Укажите в .env браузер, где вы вошли в YouTube: "
            "YTDLP_COOKIES_FROM_BROWSER=firefox (или chrome, edge)."
        )
    if "private video" in low:
        return "Это приватное видео, оно недоступно для скачивания."
    if "sign in to confirm your age" in low or "age-restricted" in low:
        return "Видео с возрастным ограничением: укажите YTDLP_COOKIES_FROM_BROWSER в .env."
    if "members-only" in low or "join this channel" in low:
        return "Видео только для спонсоров канала."
    if "is not a valid url" in low or "unsupported url" in low:
        return "Не похоже на ссылку YouTube."
    if "live event will begin" in low or "premieres in" in low:
        return "Трансляция ещё не началась."
    return re.sub(r"^ERROR:\s*", "", text).strip()[:400]


def expand(url: str, limit: int = 300) -> Expanded:
    """Превращает ссылку на видео, плейлист или канал в список видео."""
    url = url.strip()
    if _CHANNEL_ROOT.match(url.rstrip("/")):
        url = url.rstrip("/") + "/videos"
    single = _is_single_video(url)
    opts = _base_opts() | {"extract_flat": "in_playlist", "skip_download": True, "noplaylist": single}
    if not single:
        opts["playlistend"] = limit
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError(friendly_error(exc)) from exc

    if info.get("_type") not in ("playlist", "multi_video"):
        ref = _ref(info)
        if ref is None:
            raise DownloadError("Видео недоступно.")
        ref.thumbnail = _thumbnail(info)
        return Expanded("video", ref.title, [ref])

    entries: list[VideoRef] = []
    for entry in info.get("entries") or []:
        if entry.get("ie_key") == "YoutubeTab" or entry.get("_type") == "playlist":
            continue  # вложенные вкладки канала (Shorts, Live) не раскрываем
        ref = _ref(entry)
        if ref:
            entries.append(ref)
    is_channel = bool(info.get("channel_id")) and info.get("id") == info.get("channel_id")
    title = info.get("title") or info.get("channel") or url
    if is_channel:
        title = re.sub(r"\s+-\s+(Videos|Shorts|Live|Видео)$", "", title)
    return Expanded("channel" if is_channel else "playlist", title, entries)


def download_audio(url: str, dest: Path, progress: Callable[[float], None] | None = None) -> tuple[Path, dict]:
    """Скачивает только звуковую дорожку в dest/audio.* и сохраняет метаданные в dest/video.json."""
    dest.mkdir(parents=True, exist_ok=True)
    meta_file = dest / "video.json"
    cached = [p for p in dest.glob("audio.*") if p.suffix not in (".part", ".ytdl")]
    if cached and meta_file.exists():
        return cached[0], json.loads(meta_file.read_text(encoding="utf-8"))

    def hook(d: dict) -> None:
        if progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress(min(1.0, d.get("downloaded_bytes", 0) / total))

    opts = _base_opts() | {
        "format": "bestaudio/best",
        "outtmpl": str(dest / "audio.%(ext)s"),
        "noplaylist": True,
        "progress_hooks": [hook],
    }
    # Ссылка на поток привязана к IP, с которого её запросили. На сетях с IPv4 и IPv6 запрос
    # и скачивание иногда уходят разными путями, и YouTube отвечает 403. Тогда повторяем
    # с нуля строго через IPv4 — оба запроса пойдут с одного адреса.
    attempts = [{}, {"source_address": "0.0.0.0"}]
    for i, extra in enumerate(attempts):
        try:
            with yt_dlp.YoutubeDL(opts | extra) as ydl:
                info = ydl.sanitize_info(ydl.extract_info(url, download=True))
            break
        except yt_dlp.utils.DownloadError as exc:
            if "403" not in str(exc) or i == len(attempts) - 1:
                raise DownloadError(friendly_error(exc)) from exc
            log.warning("YouTube ответил 403, повторяю загрузку через IPv4")
            for part in dest.glob("audio.*"):
                part.unlink(missing_ok=True)

    path = Path((info.get("requested_downloads") or [{}])[0].get("filepath") or "")
    if not path.exists():
        found = [p for p in dest.glob("audio.*") if p.suffix not in (".part", ".ytdl")]
        if not found:
            raise DownloadError("yt-dlp не сохранил звуковую дорожку.")
        path = found[0]

    meta = _meta(info, url)
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return path, meta


def _meta(info: dict, url: str) -> dict:
    return {
        "id": info.get("id"),
        "url": info.get("webpage_url") or url,
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "channel_url": info.get("channel_url") or info.get("uploader_url"),
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),  # ГГГГММДД
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        # просмотры растут, поэтому запоминаем, когда их посчитали
        "stats_at": datetime.now().isoformat(timespec="seconds"),
        "language": info.get("language"),
        "thumbnail": _thumbnail(info),
        "description": (info.get("description") or "")[:5000],
        "chapters": [
            {"start": c.get("start_time"), "title": c.get("title")} for c in info.get("chapters") or []
        ],
    }


def fetch_meta(url: str, dest: Path) -> dict:
    """Свежие метаданные видео (дата, просмотры, лайки) без скачивания; обновляет dest/video.json."""
    try:
        with yt_dlp.YoutubeDL(_base_opts() | {"skip_download": True, "noplaylist": True}) as ydl:
            info = ydl.sanitize_info(ydl.extract_info(url, download=False))
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError(friendly_error(exc)) from exc
    meta = _meta(info, url)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "video.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta
