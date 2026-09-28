"""Сборка текста: слова с таймкодами + разметка спикеров → реплики и абзацы.

Спикер назначается целому предложению по большинству его слов, а не каждому слову
отдельно: диаризация ошибается на стыках реплик, и без этого фразы режутся между людьми.
Затем голос каждого предложения сверяется с эталонами спикеров (эмбеддингами). Это
исправляет быстрые обмены репликами, которые диаризация склеивает в одного человека.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .asr import ChunkText, Word
from .diarize import Turn

log = logging.getLogger(__name__)

PAUSE_BREAK = 3.0  # пауза, после которой начинается новый абзац
SOFT_LIMIT = 60.0  # длинный монолог делим на абзацы по концу предложения
HARD_LIMIT = 120.0
SENTENCE_END = (".", "?", "!", "…")
CLOSERS = "»\"')]"
DASHES = ("—", "–", "-")
SENTENCE_PAUSE = 1.5  # такая пауза тоже считается концом предложения
LONG_SENTENCE = 12.0  # «предложение» длиннее — скорее всего, пропущены точки
MIN_RUN = 1.0  # внутри длинного предложения куски короче не считаются сменой спикера

EMBED_MIN = 0.8  # короче этого голос по отпечатку надёжно не определить
CENTROID_MIN = 3.0  # по предложениям не короче этого строятся эталоны голосов
MARGIN_LONG = 0.10  # насколько голос должен быть ближе к другому спикеру, чтобы сменить подпись
MARGIN_SHORT = 0.20  # то же для предложений короче 2 с: там отпечаток шумнее

# отрезки (начало, конец) → нормированные эмбеддинги голоса или None, если отрезок слишком короткий
Embed = Callable[[list[tuple[float, float]]], list["np.ndarray | None"]]


@dataclass
class Segment:
    start: float
    end: float
    speaker: str | None
    words: list[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words).strip()

    @property
    def duration(self) -> float:
        return self.end - self.start


def build_segments(chunks: list[ChunkText], turns: list[Turn] | None, embed: Embed | None = None) -> list[Segment]:
    words: list[Word] = []
    for ch in chunks:
        if ch.words:
            words.extend(ch.words)
        elif ch.text:
            # движок без таймкодов слов: весь фрагмент считается одним «словом»
            words.append(Word(ch.text, ch.start, ch.end))

    if not turns:
        return _group(_sentences(words, [None] * len(words)))
    units = _sentences(words, _assign(words, turns))
    if embed is not None:
        _verify_voices(units, embed)
    return _relabel(_strip_dialogue_dashes(_group(units)))


def speaker_stats(segments: list[Segment]) -> dict[str, float]:
    stats: dict[str, float] = {}
    for seg in segments:
        if seg.speaker:
            stats[seg.speaker] = round(stats.get(seg.speaker, 0.0) + seg.duration, 1)
    return stats


# ---------- спикер для каждого слова ----------


def _assign(words: list[Word], turns: list[Turn]) -> list[str | None]:
    """Каждому слову — спикер с наибольшим пересечением по времени (или ближайший)."""
    turns = sorted(turns, key=lambda t: t.start)
    labels: list[str | None] = []
    j = 0
    for w in words:
        while j < len(turns) and turns[j].end < w.start:
            j += 1
        best, best_overlap = None, 0.0
        k = j
        while k < len(turns) and turns[k].start <= w.end:
            overlap = min(w.end, turns[k].end) - max(w.start, turns[k].start)
            if overlap > best_overlap:
                best, best_overlap = turns[k].speaker, overlap
            k += 1
        if best is None and turns:
            mid = (w.start + w.end) / 2
            near = min(turns[max(0, j - 1) : j + 1], key=lambda t: min(abs(t.start - mid), abs(t.end - mid)))
            if min(abs(near.start - mid), abs(near.end - mid)) <= 1.0:
                best = near.speaker
        labels.append(best if best is not None else (labels[-1] if labels else None))
    return labels


# ---------- предложения ----------


def _is_dash(word: Word) -> bool:
    return word.text.strip() in DASHES


def _ends_sentence(word: Word) -> bool:
    return word.text.rstrip().rstrip(CLOSERS).endswith(SENTENCE_END)


def _sentences(words: list[Word], labels: list[str | None]) -> list[Segment]:
    """Режет поток слов на предложения: по знакам конца предложения, тире диалога и паузам."""
    out: list[Segment] = []
    cur: list[int] = []
    for i, w in enumerate(words):
        if cur and (_is_dash(w) or w.start - words[cur[-1]].end > SENTENCE_PAUSE):
            out.extend(_vote([words[k] for k in cur], [labels[k] for k in cur]))
            cur = []
        cur.append(i)
        if _ends_sentence(w):
            out.extend(_vote([words[k] for k in cur], [labels[k] for k in cur]))
            cur = []
    if cur:
        out.extend(_vote([words[k] for k in cur], [labels[k] for k in cur]))
    return out


def _vote(words: list[Word], labels: list[str | None]) -> list[Segment]:
    """Спикер предложения — тот, чьи слова в нём звучат дольше всех."""
    if words[-1].end - words[0].start <= LONG_SENTENCE:
        weight: dict[str | None, float] = {}
        for w, label in zip(words, labels):
            weight[label] = weight.get(label, 0.0) + max(w.end - w.start, 0.05)
        best = max(weight, key=weight.get)
        return [Segment(words[0].start, words[-1].end, best, list(words))]

    # Очень длинное «предложение» — скорее всего, пропущены точки, и внутри может быть смена
    # говорящего. Оставляем пословную разметку, но короткие скачки приклеиваем к соседям.
    runs: list[tuple[str | None, list[Word]]] = []
    for w, label in zip(words, labels):
        if runs and runs[-1][0] == label:
            runs[-1][1].append(w)
        else:
            runs.append((label, [w]))
    merged: list[tuple[str | None, list[Word]]] = []
    for label, ws in runs:
        if merged and (label == merged[-1][0] or ws[-1].end - ws[0].start < MIN_RUN):
            merged[-1][1].extend(ws)
        else:
            merged.append((label, list(ws)))
    return [Segment(ws[0].start, ws[-1].end, label, ws) for label, ws in merged]


# ---------- сверка голоса ----------


def _voice_span(seg: Segment) -> tuple[float, float]:
    words = [w for w in seg.words if not _is_dash(w)] or seg.words
    return words[0].start, words[-1].end


def _verify_voices(units: list[Segment], embed: Embed) -> int:
    """Переподписывает предложения, голос которых явно ближе к другому спикеру.

    Эталон голоса — среднее отпечатков длинных предложений спикера. Подпись меняется, только
    если другой эталон заметно ближе: пересказ чужих слов тем же голосом останется на месте.
    """
    cand = [u for u in units if u.speaker is not None and u.duration >= EMBED_MIN]
    if not cand:
        return 0
    embs = embed([_voice_span(u) for u in cand])
    groups: dict[str, list[np.ndarray]] = {}
    for u, emb in zip(cand, embs):
        if emb is not None and u.duration >= CENTROID_MIN:
            groups.setdefault(u.speaker, []).append(emb)
    if len(groups) < 2:
        return 0
    names = list(groups)
    centroids = np.stack([np.mean(groups[n], axis=0) for n in names])
    centroids /= np.linalg.norm(centroids, axis=1, keepdims=True) + 1e-9

    fixed = 0
    for u, emb in zip(cand, embs):
        if emb is None or u.speaker not in groups:
            continue  # у редкого спикера нет эталона — не трогаем
        sims = dict(zip(names, (centroids @ emb).tolist()))
        best = max(sims, key=sims.get)
        margin = MARGIN_LONG if u.duration >= 2.0 else MARGIN_SHORT
        if best != u.speaker and sims[best] - sims[u.speaker] >= margin:
            u.speaker = best
            fixed += 1
    log.info("voice check: %d sentences checked, %d reassigned", len(cand), fixed)
    return fixed


# ---------- реплики ----------


def _should_break(seg: Segment, word: Word) -> bool:
    if word.start - seg.end > PAUSE_BREAK:
        return True
    duration = word.end - seg.start
    return (duration > SOFT_LIMIT and _ends_sentence(seg.words[-1])) or duration > HARD_LIMIT


def _group(units: list[Segment]) -> list[Segment]:
    """Склеивает подряд идущие предложения одного спикера в реплики, длинные монологи — в абзацы."""
    out: list[Segment] = []
    for u in units:
        last = out[-1] if out else None
        if last and last.speaker == u.speaker and not _should_break(last, u.words[0]):
            last.words.extend(u.words)
            last.end = max(last.end, u.end)
        else:
            out.append(Segment(u.start, u.end, u.speaker, list(u.words)))
    return out


def _strip_dialogue_dashes(segments: list[Segment]) -> list[Segment]:
    """GigaAM отмечает смену говорящего тире («— Где? — Здесь…»). Когда спикеры размечены,
    тире на границах реплик лишние; тире внутри реплики оставляем."""
    result = []
    for seg in segments:
        words = seg.words
        while words and _is_dash(words[0]):
            words = words[1:]
        while words and _is_dash(words[-1]):
            words = words[:-1]
        if words:
            result.append(Segment(words[0].start, words[-1].end, seg.speaker, words))
    return result


def _relabel(segments: list[Segment]) -> list[Segment]:
    """SPEAKER_03 → S1, S2… в порядке первого появления в тексте."""
    mapping: dict[str, str] = {}
    for seg in segments:
        if seg.speaker is not None and seg.speaker not in mapping:
            mapping[seg.speaker] = f"S{len(mapping) + 1}"
        if seg.speaker is not None:
            seg.speaker = mapping[seg.speaker]
    return segments
