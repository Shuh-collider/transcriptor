"""Обработка одного видео: ссылка → звук → текст → спикеры → документ transcript.json."""

from __future__ import annotations

import gc
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Callable

import torch

from . import config
from .asr import GigaAMEngine, QwenEngine
from .diarize import TITLE as DIARIZATION_TITLE
from .diarize import DiarizationUnavailable, Diarizer
from .media import SAMPLE_RATE, load_audio
from .merge import build_segments, speaker_stats
from .summarize import summarize
from .vad import speech_chunks
from .youtube import download_audio, fetch_meta

log = logging.getLogger(__name__)

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

# Доли общего прогресса по этапам (для одной полосы в интерфейсе)
STAGES = {"download": 0.08, "prepare": 0.07, "language": 0.03, "asr": 0.52, "diarize": 0.30}


class Cancelled(Exception):
    pass


@dataclass
class Options:
    language: str = "auto"  # auto | ru | en
    ru_engine: str = "gigaam"  # gigaam | qwen
    diarize: bool = True
    num_speakers: int | None = None
    summarize: bool = True
    summary_model: str = field(default_factory=config.default_summary_model)
    summary_language: str = "ru"  # ru | en | same

    @classmethod
    def from_dict(cls, data: dict | None) -> "Options":
        data = data or {}
        known = {f.name for f in fields(cls)}
        opts = cls(**{k: v for k, v in data.items() if k in known and v is not None})
        if opts.language not in ("auto", "ru", "en"):
            opts.language = "auto"
        if opts.ru_engine not in ("gigaam", "qwen"):
            opts.ru_engine = "gigaam"
        if opts.summary_model not in config.SUMMARY_MODELS:
            opts.summary_model = config.default_summary_model()
        if opts.summary_language not in ("ru", "en", "same"):
            opts.summary_language = "ru"
        try:
            opts.num_speakers = int(opts.num_speakers) if opts.num_speakers else None
        except (TypeError, ValueError):
            opts.num_speakers = None
        if opts.num_speakers is not None and not 1 <= opts.num_speakers <= 20:
            opts.num_speakers = None
        return opts

    def to_dict(self) -> dict:
        return asdict(self)


class Models:
    """Модели на видеокарте. Загружаются при первой необходимости и живут между заданиями."""

    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self._lock = threading.Lock()
        self._gigaam: GigaAMEngine | None = None
        self._qwen: QwenEngine | None = None
        self._diarizer: Diarizer | None = None

    def gigaam(self) -> GigaAMEngine:
        with self._lock:
            if self._gigaam is None:
                self._gigaam = GigaAMEngine(self.device)
            return self._gigaam

    def qwen(self) -> QwenEngine:
        with self._lock:
            if self._qwen is None:
                self._qwen = QwenEngine(self.device)
            return self._qwen

    def diarizer(self) -> Diarizer:
        with self._lock:
            if self._diarizer is None:
                self._diarizer = Diarizer(self.device)
            return self._diarizer

    def loaded(self) -> list[str]:
        names = []
        if self._gigaam:
            names.append(GigaAMEngine.title)
        if self._qwen:
            names.append(QwenEngine.title)
        if self._diarizer:
            names.append(DIARIZATION_TITLE)
        return names

    def unload(self) -> None:
        with self._lock:
            self._gigaam = self._qwen = self._diarizer = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def _free_gpu_cache() -> None:
    """Отдаёт видеокарте память, которую PyTorch держит про запас после генерации текста.

    Иначе следующему этапу не хватает видеопамяти, и Windows молча переносит её часть
    в обычную память: работа замедляется в разы вместо понятной ошибки.
    """
    if torch.cuda.is_available():
        gc.collect()
        torch.cuda.empty_cache()


def video_dir(video_id: str) -> Path:
    if not VIDEO_ID.match(video_id or ""):
        raise ValueError(f"Некорректный id видео: {video_id!r}")
    return config.VIDEOS_DIR / video_id


def load_doc(video_id: str) -> dict | None:
    path = video_dir(video_id) / "transcript.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save_doc(doc: dict) -> None:
    path = video_dir(doc["video"]["id"]) / "transcript.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


Report = Callable[[str, float, str], None]


def transcribe_video(
    video_id: str,
    url: str,
    options: Options,
    models: Models,
    report: Report,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict:
    """Скачивает и расшифровывает видео, сохраняет transcript.json и возвращает документ."""

    def stage(name: str, message: str) -> Callable[[float], None]:
        def update(frac: float) -> None:
            if cancelled():
                raise Cancelled()
            report(name, max(0.0, min(1.0, frac)), message)

        update(0.0)
        return update

    timings: dict[str, float] = {}
    notes: list[str] = []
    config.reload_env()  # ключи могли вписать в .env, пока сервер работал

    t = time.time()
    audio_path, meta = download_audio(url, video_dir(video_id), stage("download", "Скачиваю звук с YouTube"))
    timings["download"] = round(time.time() - t, 1)

    t = time.time()
    update = stage("prepare", "Ищу фрагменты речи")
    audio = load_audio(audio_path)
    meta["duration"] = meta.get("duration") or round(len(audio) / SAMPLE_RATE, 1)
    chunks = speech_chunks(audio, progress=update)
    timings["prepare"] = round(time.time() - t, 1)

    language = options.language
    if language == "auto" and chunks:
        t = time.time()
        stage("language", "Определяю язык")
        language = models.qwen().detect_language(audio, chunks)
        timings["language"] = round(time.time() - t, 1)
        if language is None:
            yt_lang = (meta.get("language") or "")[:2].lower()
            language = yt_lang or "ru"
    elif language == "auto":
        language = (meta.get("language") or "ru")[:2].lower()

    engine = models.gigaam() if language == "ru" and options.ru_engine == "gigaam" else models.qwen()
    if language not in engine.languages:
        notes.append(f"Язык «{language}» не поддерживается, распознаю с автоопределением.")
    t = time.time()
    update = stage("asr", f"Распознаю речь: {engine.title}")
    chunk_texts = engine.transcribe(
        audio, chunks, language=language if language in engine.languages else None, progress=update
    )
    timings["asr"] = round(time.time() - t, 1)
    _free_gpu_cache()

    turns = None
    diarization = None
    embed = None
    if options.diarize and chunks:
        t = time.time()
        update = stage("diarize", "Определяю, кто говорит")
        try:
            diarizer = models.diarizer()
            turns = diarizer(audio, num_speakers=options.num_speakers, progress=update)
            diarization = DIARIZATION_TITLE
            embed = lambda spans: diarizer.embed(audio, spans)  # noqa: E731 — уточнение по тире
        except DiarizationUnavailable as exc:
            notes.append(str(exc))
        timings["diarize"] = round(time.time() - t, 1)
    if cancelled():
        raise Cancelled()

    segments = build_segments(chunk_texts, turns, embed)
    _free_gpu_cache()
    if not segments:
        notes.append("В видео не найдено речи.")
    stats = speaker_stats(segments)
    old = load_doc(video_id) or {}
    old_names = {k: v.get("name", "") for k, v in (old.get("speakers") or {}).items()}

    doc = {
        "version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "video": meta,
        "language": language,
        "engine": engine.title,
        "diarization": diarization,
        "options": options.to_dict(),
        "speakers": {label: {"name": old_names.get(label, ""), "seconds": sec} for label, sec in stats.items()},
        "segments": [
            {
                "start": round(s.start, 2),
                "end": round(s.end, 2),
                "speaker": s.speaker,
                "text": s.text,
                "words": [[round(w.start, 2), round(w.end, 2), w.text] for w in s.words],
            }
            for s in segments
        ],
        "summary": None,
        "summary_error": None,
        "notes": notes,
        "timings": timings,
    }
    save_doc(doc)
    return doc


def _label_order(label: str) -> int:
    return int(label[1:]) if label[1:].isdigit() else 10_000


def merge_same_name_speakers(doc: dict) -> list[list[str]]:
    """Объединяет спикеров с одинаковым именем: обычно это один человек, которого диаризация
    разбила на двоих (например, его вопросы записаны отдельно, в другой акустике).

    Реплики переходят к первой метке; исходная метка остаётся в orig_speaker, чтобы объединение
    можно было отменить. Возвращает пары [откуда, куда].
    """
    speakers = doc.get("speakers") or {}
    first: dict[str, str] = {}
    merges: list[list[str]] = []
    for label in sorted(speakers, key=_label_order):
        name = " ".join((speakers[label].get("name") or "").split()).casefold()
        if not name:
            continue
        if name in first:
            merges.append([label, first[name]])
        else:
            first[name] = label
    if not merges:
        return []
    into = dict(merges)
    for seg in doc.get("segments") or []:
        target = into.get(seg.get("speaker"))
        if target:
            seg.setdefault("orig_speaker", seg["speaker"])
            seg["speaker"] = target
    summary_speakers = (doc.get("summary") or {}).get("speakers") or {}
    for src, dst in merges:
        info = speakers.pop(src)
        speakers[dst]["seconds"] = round(speakers[dst].get("seconds", 0) + info.get("seconds", 0), 1)
        if not speakers[dst].get("role") and info.get("role"):
            speakers[dst]["role"] = info["role"]
        summary_speakers.pop(src, None)
    doc.setdefault("speaker_merges", []).extend({"from": src, "into": dst} for src, dst in merges)
    return merges


def refresh_video_meta(video_id: str) -> dict:
    """Заново берёт с YouTube дату, просмотры и лайки (без скачивания звука) и дописывает в документ."""
    doc = load_doc(video_id)
    url = ((doc or {}).get("video") or {}).get("url") or f"https://www.youtube.com/watch?v={video_id}"
    meta = fetch_meta(url, video_dir(video_id))
    doc = load_doc(video_id)  # за время запроса документ мог обновиться
    if doc is None:
        return meta
    doc["video"].update({k: v for k, v in meta.items() if v is not None})
    save_doc(doc)
    return doc["video"]


def summarize_video(video_id: str, model: str, language: str, progress: Callable[[str], None]) -> dict:
    """Делает пересказ для уже расшифрованного видео и дописывает его в transcript.json."""
    doc = load_doc(video_id)
    if doc is None:
        raise FileNotFoundError("Расшифровка не найдена.")
    out_language = doc.get("language") if language == "same" else language
    t = time.time()
    summary = summarize(doc, model, out_language if out_language in ("ru", "en") else "ru", progress)
    doc = load_doc(video_id) or doc  # имена спикеров могли поменять, пока шёл запрос
    doc["summary"] = summary
    doc["summary_error"] = None
    doc.setdefault("timings", {})["summary"] = round(time.time() - t, 1)
    for label, info in summary["speakers"].items():
        if label in doc["speakers"] and not doc["speakers"][label].get("name") and info.get("name"):
            doc["speakers"][label]["name"] = info["name"]
        if label in doc["speakers"]:
            doc["speakers"][label]["role"] = info.get("role", "")
    merge_same_name_speakers(doc)
    save_doc(doc)
    return doc
