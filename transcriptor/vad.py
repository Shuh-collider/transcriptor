"""Нарезка звука на фрагменты речи по паузам (Silero VAD)."""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from importlib import resources
from typing import Callable

import numpy as np
import torch
from silero_vad.utils_vad import get_speech_timestamps_from_probs

from .media import SAMPLE_RATE

WINDOW = 512  # окно Silero при 16 кГц (32 мс)


@dataclass(frozen=True)
class Chunk:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


_model = None
_lock = threading.Lock()


def _load_model():
    global _model
    if _model is None:
        # torch.jit.load открывает файл через C++ fopen, который не понимает кириллицу
        # в пути к проекту, поэтому модель читается в память средствами Python
        data = resources.files("silero_vad").joinpath("data/silero_vad.jit").read_bytes()
        _model = torch.jit.load(io.BytesIO(data), map_location="cpu").eval()
    return _model


def _speech_probs(audio: np.ndarray, streams: int, progress: Callable[[float], None] | None) -> list[float]:
    """Вероятность речи для каждого окна.

    Silero обрабатывает окна строго по очереди, поэтому запись делится на несколько равных
    частей, которые идут через модель параллельно одной пачкой: у каждой строки пачки своё
    внутреннее состояние. Так в разы быстрее, а на стыках частей теряется не больше окна.
    """
    model = _load_model()
    n = -(-len(audio) // WINDOW)
    streams = max(1, min(streams, n // 256))
    per = -(-n // streams)
    padded = np.zeros(per * streams * WINDOW, dtype=np.float32)
    padded[: len(audio)] = audio
    frames = torch.from_numpy(padded).view(streams, per, WINDOW)
    probs = torch.empty(streams, per)
    model.reset_states()  # состояние под размер пачки модель создаст на первом шаге
    with torch.inference_mode():
        for i in range(per):
            probs[:, i] = model(frames[:, i], SAMPLE_RATE).squeeze(-1)
            if progress and i % 256 == 0:
                progress(i / per)
    return probs.reshape(-1)[:n].tolist()


def speech_chunks(
    audio: np.ndarray,
    max_chunk: float = 22.0,
    max_gap: float = 1.5,
    progress: Callable[[float], None] | None = None,
    streams: int = 16,
) -> list[Chunk]:
    """Находит речь и склеивает соседние куски во фрагменты не длиннее max_chunk секунд.

    Границы фрагментов всегда проходят по паузам, чтобы не разрезать слова.
    """
    with _lock:
        probs = _speech_probs(audio, streams, progress)
    stamps = get_speech_timestamps_from_probs(
        probs,
        sampling_rate=SAMPLE_RATE,
        threshold=0.45,
        min_speech_duration_ms=200,
        min_silence_duration_ms=250,
        speech_pad_ms=200,
        max_speech_duration_s=max_chunk,
        audio_length_samples=len(audio),
    )
    total = len(audio) / SAMPLE_RATE
    spans = [(s["start"] / SAMPLE_RATE, min(s["end"] / SAMPLE_RATE, total)) for s in stamps]

    chunks: list[Chunk] = []
    cur_start, cur_end = None, None
    for start, end in spans:
        if cur_start is None:
            cur_start, cur_end = start, end
        elif start - cur_end <= max_gap and end - cur_start <= max_chunk:
            cur_end = max(cur_end, end)
        else:
            chunks.append(Chunk(cur_start, cur_end))
            cur_start, cur_end = start, end
    if cur_start is not None:
        chunks.append(Chunk(cur_start, cur_end))
    return chunks
