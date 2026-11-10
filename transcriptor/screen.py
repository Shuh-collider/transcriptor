"""Надписи на экране: кадры в моменты смены картинки → Gemini читает текст → строки с таймкодами."""

from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image
from pydantic import BaseModel, ValidationError

from . import config
from .export import fmt_ts
from .i18n import L
from .media import ffmpeg_exe
from .summarize import SummaryError, _object, call_gemini
from .youtube import DownloadError, download_video

log = logging.getLogger(__name__)

MODEL = "gemini-3.8-flash"
SCENE = 0.12  # смена картинки (0–1), при которой кадр берётся сразу
STEP = 1.0  # и, кроме того, кадр каждую секунду: итог анимации часто держится на экране 1–2 с
SAME_SCREEN = 6.0  # средняя разница яркости (0–255) с началом экрана, ниже которой кадр — тот же экран
MAX_FRAMES = 240
BATCH = 24  # кадров в одном запросе к Gemini

Progress = Callable[[float, str], None]


def _NOT_READ() -> str:
    return L("On-screen text was not read: ", "Надписи на экране не прочитаны: ")


class ScreenError(RuntimeError):
    pass


class ScreenCancelled(Exception):
    pass


class _Item(BaseModel):
    frame: int
    text: str


class _Result(BaseModel):
    items: list[_Item]


SCHEMA = _object(
    {"items": {"type": "array", "items": _object({"frame": {"type": "integer"}, "text": {"type": "string"}})}}
)

SYSTEM = """You read the text shown on screen in frames taken from a YouTube video at the moments the picture changes. Each image is preceded by its frame number and timestamp.

Return the meaningful on-screen text, so that someone who cannot see the video knows what was written on it:
- titles, captions, slide text, lists, labels, numbers and units in charts and tables, prompts, chat messages, code and interface text that the video is showing;
- keep the original language and wording: do not translate, summarize or explain; keep line breaks where they separate items;
- report a frame only when its text is new or changed compared with the previous frame and with the text already reported before these frames; if a frame only adds lines to text already shown, report just the new lines;
- skip channel logos, watermarks, the video player interface, decorative text that stays on screen, and burned-in subtitles of spoken dialogue (speech is transcribed separately);
- skip text you cannot read reliably rather than guess;
- for very long text (code, documents, dense interfaces) write at most about 1500 characters per frame and end with "…".

`frame` is the frame number. If no frame has meaningful new text, return an empty list."""


def extract_frames(video: Path, out_dir: Path, cancelled: Callable[[], bool]) -> list[tuple[float, Path]]:
    """По кадру на каждый «экран» видео. Результат кэшируется в out_dir/frames.json.

    Кадры берутся при каждой смене картинки и раз в STEP секунд, потом похожие подряд кадры
    склеиваются в один экран. От экрана остаётся последний кадр: если текст печатается или
    появляется по строчкам, к концу экрана он уже целиком. Время — начало экрана.
    """
    index = out_dir / "frames.json"
    if index.exists():
        frames = [(t, out_dir / name) for t, name in json.loads(index.read_text(encoding="utf-8"))]
        if all(p.exists() for _, p in frames):
            return frames
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink()

    expr = f"isnan(prev_selected_t)+gt(scene,{SCENE})+gte(t-prev_selected_t,{STEP})"
    cmd = [
        ffmpeg_exe(), "-hide_banner", "-nostats",
        "-hwaccel", "auto",  # декодирование на видеокарте, если получится; иначе на процессоре
        "-i", str(video),
        # 4 кадра в секунду вместо 25–60: сравнивать соседние кадры так в разы быстрее
        "-vf", f"fps=4,select='{expr}',showinfo,scale='min(1280,iw)':-2",
        "-fps_mode", "vfr", "-q:v", "3",
        str(out_dir / "%05d.jpg"),
    ]
    log_path = out_dir / "ffmpeg.log"
    with log_path.open("w", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        while proc.poll() is None:
            if cancelled():
                proc.kill()
                raise ScreenCancelled()
            time.sleep(0.3)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        log.warning("ffmpeg frames failed:\n%s", text[-2000:])
        raise ScreenError(L("ffmpeg could not split the video into frames.", "ffmpeg не смог разобрать видео на кадры."))
    times = [float(t) for t in re.findall(r"Parsed_showinfo.*?pts_time:\s*([\d.]+)", text)]
    frames = _one_per_screen(list(zip(times, sorted(out_dir.glob("*.jpg")))))
    if len(frames) > MAX_FRAMES:  # оставляем равномерно
        keep = {round(i * (len(frames) - 1) / (MAX_FRAMES - 1)) for i in range(MAX_FRAMES)}
        frames = [f for i, f in enumerate(frames) if i in keep]
    kept = {p for _, p in frames}
    for path in out_dir.glob("*.jpg"):
        if path not in kept:
            path.unlink()
    index.write_text(json.dumps([[round(t, 2), p.name] for t, p in frames]), encoding="utf-8")
    log_path.unlink(missing_ok=True)
    return frames


def _thumb(path: Path) -> np.ndarray:
    with Image.open(path) as img:
        return np.asarray(img.convert("L").resize((96, 54)), dtype=np.float32)


def _one_per_screen(frames: list[tuple[float, Path]]) -> list[tuple[float, Path]]:
    """Склеивает похожие подряд кадры в экраны: (время начала экрана, его последний кадр)."""
    screens: list[tuple[float, Path]] = []
    anchor = None
    for t, path in frames:
        thumb = _thumb(path)
        if anchor is None or np.abs(thumb - anchor).mean() > SAME_SCREEN:
            anchor = thumb
            screens.append((t, path))
        else:
            screens[-1] = (screens[-1][0], path)
    return screens


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _continues(prev: str, new: str) -> bool:
    """new — это prev, который допечатали дальше (печать по буквам, появление строк)."""
    tail = _norm(prev).rstrip("|▌_…. ")[-80:]
    return len(tail) >= 40 and len(new) > len(prev) and tail in _norm(new)


def read_frames(
    frames: list[tuple[float, Path]], progress: Progress, cancelled: Callable[[], bool], hours: bool = False
) -> list[dict]:
    """Отправляет кадры в Gemini пачками и возвращает [{start, text}] — только новый текст."""
    items: list[dict] = []
    total = len(frames)
    for first in range(0, total, BATCH):
        if cancelled():
            raise ScreenCancelled()
        batch = frames[first : first + BATCH]
        last = min(first + BATCH, total)
        say = lambda msg, a=first + 1, b=last: progress(first / total, L(f"On-screen text, frames {a}–{b} of {total}: {msg}", f"Надписи, кадры {a}–{b} из {total}: {msg}"))  # noqa: E731
        content: list[dict] = []
        if items:
            content.append({"type": "text", "text": "Text already reported just before these frames:\n" + items[-1]["text"]})
        for n, (t, path) in enumerate(batch, start=first + 1):
            content.append({"type": "text", "text": f"Frame {n} at {fmt_ts(t, hours)}"})
            content.append({
                "type": "image",
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                "mime_type": "image/jpeg",
                "resolution": "high",
            })
        try:
            text, *_ = call_gemini(
                SYSTEM, content, MODEL, say, schema=SCHEMA, thinking="low",
                reading=L("Gemini is looking at the frames…", "Gemini смотрит кадры…"),
                writing=L("Gemini is writing out the text…", "Gemini выписывает надписи…"),
            )
            result = _Result.model_validate_json(re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text))
        except SummaryError as exc:
            raise ScreenError(_NOT_READ() + str(exc)) from exc
        except ValidationError as exc:
            log.warning("unexpected screen JSON: %s", text[:500])
            raise ScreenError(_NOT_READ() + L("Gemini answered in an unexpected format.", "Gemini ответил в неожиданном формате.")) from exc
        times = {n: t for n, (t, _) in enumerate(batch, start=first + 1)}
        for item in sorted(result.items, key=lambda i: i.frame):
            body = item.text.strip()
            if item.frame not in times or not body:
                continue
            if items and _continues(items[-1]["text"], body):
                items[-1]["text"] = body  # текст допечатывался: оставляем полный, время — начало
                continue
            if items and _norm(items[-1]["text"]) == _norm(body):
                continue
            items.append({"start": times[item.frame], "text": body})
    progress(1.0, L("On-screen text read", "Надписи прочитаны"))
    return items


def read_screen(url: str, dest: Path, progress: Progress, cancelled: Callable[[], bool], hours: bool = False) -> list[dict]:
    """Скачивает картинку видео, выбирает кадры и читает с них надписи. Видео после разбора удаляется."""
    config.reload_env()
    if not config.gemini_configured():
        raise ScreenError(_NOT_READ() + L("this needs GEMINI_API_KEY in the .env file.", "для этого нужен GEMINI_API_KEY в файле .env."))

    def downloading(frac: float) -> None:
        if cancelled():
            raise ScreenCancelled()
        progress(0.25 * frac, L("Downloading the video picture", "Скачиваю видео для надписей"))

    frames_dir = dest / "frames"
    if not (frames_dir / "frames.json").exists():
        try:
            video = download_video(url, dest, downloading)
        except DownloadError as exc:
            raise ScreenError(_NOT_READ() + str(exc)) from exc
        progress(0.25, L("Picking frames where the picture changes", "Выбираю кадры, где меняется картинка"))
        frames = extract_frames(video, frames_dir, cancelled)
        video.unlink(missing_ok=True)  # кадры уже есть, а видео занимает сотни мегабайт
    else:
        frames = extract_frames(Path(), frames_dir, cancelled)
    if not frames:
        return []
    return read_frames(frames, lambda f, msg: progress(0.3 + 0.7 * f, msg), cancelled, hours)
