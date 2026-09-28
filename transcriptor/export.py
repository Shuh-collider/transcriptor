"""Время в тексте и выгрузка расшифровки в TXT и Markdown."""

from __future__ import annotations

import re


def fmt_ts(seconds: float, hours: bool | None = None) -> str:
    """0:05, 12:34 или 1:02:03."""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if hours or (hours is None and h):
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


_TS = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,]\d+)?\s*$")


def parse_ts(text: str) -> float | None:
    m = _TS.match(str(text))
    if not m:
        return None
    h, mnt, sec = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    return float(h * 3600 + mnt * 60 + sec)


def fmt_date(value: str | None) -> str:
    """'20120317' или '2026-09-28T02:10:00' → '17.03.2012'."""
    digits = re.sub(r"\D", "", str(value or ""))[:8]
    return f"{digits[6:8]}.{digits[4:6]}.{digits[:4]}" if len(digits) == 8 else ""


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 10 < n < 20:
        return many
    n %= 10
    return one if n == 1 else few if 1 < n < 5 else many


def fmt_count(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def video_stats(video: dict) -> str:
    """'загружено 17.03.2012 · 82 169 просмотров · 761 лайк (на 28.09.2026)'."""
    parts = []
    if video.get("upload_date"):
        parts.append(f"загружено {fmt_date(video['upload_date'])}")
    views, likes = video.get("view_count"), video.get("like_count")
    if views is not None:
        parts.append(f"{fmt_count(views)} {plural_ru(views, 'просмотр', 'просмотра', 'просмотров')}")
    if likes is not None:
        parts.append(f"{fmt_count(likes)} {plural_ru(likes, 'лайк', 'лайка', 'лайков')}")
    text = " · ".join(parts)
    if views is not None and video.get("stats_at"):
        text += f" (на {fmt_date(video['stats_at'])})"
    return text


_URL = re.compile(
    r"(?:https?://|www\.)[^\s<>\"'«»]+"
    r"|\b(?:t\.me|vk\.com|instagram\.com|tiktok\.com|boosty\.to|discord\.gg|patreon\.com)/[^\s<>\"'«»]+",
    re.IGNORECASE,
)
_TRAILING = ".,;:!?]}»\"'"
_LABEL_EDGES = " \t:-–—|•·►▶➤→⇒👉⬇↓(["


def _clean_url(url: str) -> str:
    while url and (url[-1] in _TRAILING or (url[-1] == ")" and url.count("(") < url.count(")"))):
        url = url[:-1]
    return url


def description_links(text: str | None) -> list[dict]:
    """Ссылки из описания видео. Подпись — текст перед ссылкой в той же строке
    («Telegram: https://…»), а если его нет — короткая строка над ссылкой."""
    links: list[dict] = []
    seen: set[str] = set()
    prev = ""
    for line in (text or "").splitlines():
        pos = 0
        for m in _URL.finditer(line):
            url = _clean_url(m.group(0))
            href = url if url.lower().startswith("http") else f"https://{url}"
            key = href.rstrip("/").lower()
            label = line[pos : m.start()].strip(_LABEL_EDGES)
            pos = m.end()
            if key in seen:
                continue
            seen.add(key)
            if not label and prev and len(prev) <= 80 and not _URL.search(prev):
                label = prev.strip(_LABEL_EDGES)
            links.append({"url": href, "label": label[:100]})
        if line.strip():
            prev = line.strip()
    return links


def speaker_name(doc: dict, label: str | None) -> str:
    if not label:
        return ""
    info = (doc.get("speakers") or {}).get(label) or {}
    return info.get("name") or f"Спикер {label.lstrip('S')}"


def _long(doc: dict) -> bool:
    return (doc.get("video") or {}).get("duration", 0) >= 3600


def to_txt(doc: dict) -> str:
    video = doc.get("video") or {}
    hours = _long(doc)
    lines = [video.get("title") or "", video.get("url") or ""]
    if video_stats(video):
        lines.append(video_stats(video))
    lines.append("")
    summary = doc.get("summary")
    if summary:
        lines += ["КРАТКО", summary["tldr"], "", "ПЕРЕСКАЗ", summary["summary"], ""]
        if summary.get("key_points"):
            lines += ["ГЛАВНОЕ"] + [f"• {p}" for p in summary["key_points"]] + [""]
        if summary.get("chapters"):
            lines += ["ГЛАВЫ"] + [f"{fmt_ts(c['start'], hours)} {c['title']}" for c in summary["chapters"]] + [""]
    links = description_links(video.get("description"))
    if links:
        lines += ["ССЫЛКИ ИЗ ОПИСАНИЯ"] + [f"• {l['label']}: {l['url']}" if l["label"] else f"• {l['url']}" for l in links] + [""]
    if summary or links:
        lines += ["РАСШИФРОВКА", ""]
    for seg in doc.get("segments") or []:
        who = speaker_name(doc, seg.get("speaker"))
        head = f"[{fmt_ts(seg['start'], hours)}]" + (f" {who}:" if who else "")
        lines += [f"{head} {seg['text']}", ""]
    return "\n".join(lines).strip() + "\n"


def to_markdown(doc: dict) -> str:
    video = doc.get("video") or {}
    hours = _long(doc)
    url = video.get("url") or ""
    out = [f"# {video.get('title') or 'Расшифровка'}", ""]
    meta = [x for x in (video.get("channel"), fmt_ts(video.get("duration") or 0, hours), video_stats(video), url) if x]
    out += [" · ".join(meta), ""]
    summary = doc.get("summary")
    if summary:
        out += ["## Кратко", "", summary["tldr"], "", "## Пересказ", "", summary["summary"], ""]
        if summary.get("key_points"):
            out += ["## Главное", ""] + [f"- {p}" for p in summary["key_points"]] + [""]
        if summary.get("chapters"):
            out += ["## Главы", ""]
            for c in summary["chapters"]:
                link = f"{url}&t={int(c['start'])}s" if "watch?v=" in url else url
                out.append(f"- [{fmt_ts(c['start'], hours)}]({link}) **{c['title']}** — {c['summary']}")
            out.append("")
    links = description_links(video.get("description"))
    if links:
        out += ["## Ссылки из описания", ""]
        out += [f"- [{l['label'] or l['url']}]({l['url']})" for l in links] + [""]
    out += ["## Расшифровка", ""]
    for seg in doc.get("segments") or []:
        who = speaker_name(doc, seg.get("speaker"))
        head = f"**[{fmt_ts(seg['start'], hours)}]" + (f" {who}:**" if who else "**")
        out += [f"{head} {seg['text']}", ""]
    return "\n".join(out).strip() + "\n"
