"""Кто говорит: pyannote speaker-diarization-community-1."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import numpy as np
import torch

from . import config
from .media import SAMPLE_RATE

log = logging.getLogger(__name__)

MODEL_REPO = "pyannote/speaker-diarization-community-1"
TITLE = "pyannote community-1"


class DiarizationUnavailable(RuntimeError):
    pass


@dataclass
class Turn:
    start: float
    end: float
    speaker: str


class _Hook:
    """Переводит шаги pyannote в общий прогресс 0..1."""

    def __init__(self, progress: Callable[[float], None]):
        self.progress = progress

    def __call__(self, step_name, step_artifact, file=None, total=None, completed=None):
        if total:
            frac = (completed or 0) / total
            if step_name == "segmentation":
                self.progress(0.35 * frac)
            elif step_name == "embeddings":
                self.progress(0.35 + 0.6 * frac)


class Diarizer:
    def __init__(self, device: str = "cuda"):
        token = config.hf_token()
        if not token:
            raise DiarizationUnavailable(
                "Спикеры не определены: в файле .env нет HF_TOKEN (бесплатный токен Hugging Face)."
            )
        from pyannote.audio import Pipeline

        try:
            pipeline = Pipeline.from_pretrained(MODEL_REPO, token=token)
        except Exception as exc:  # noqa: BLE001 — pyannote/HF поднимают разные исключения
            log.warning("pyannote load failed: %s", exc)
            text = str(exc).lower()
            if "gated repo" in text or "authorized list" in text:
                raise DiarizationUnavailable(
                    "Спикеры не определены: токен Hugging Face рабочий, но условия модели не приняты. Откройте "
                    f"https://huggingface.co/{MODEL_REPO} под тем же аккаунтом и нажмите «Agree and access repository»."
                ) from exc
            if "401" in text or "invalid" in text and "token" in text:
                raise DiarizationUnavailable(
                    "Спикеры не определены: Hugging Face не принял HF_TOKEN. Создайте новый токен типа Read."
                ) from exc
            pipeline = None
        if pipeline is None:
            raise DiarizationUnavailable(
                "Спикеры не определены: нет доступа к модели pyannote. Проверьте HF_TOKEN и примите условия "
                f"на https://huggingface.co/{MODEL_REPO}"
            )
        self.pipeline = pipeline.to(torch.device(device))

    def __call__(
        self,
        audio: np.ndarray,
        num_speakers: int | None = None,
        progress: Callable[[float], None] | None = None,
    ) -> list[Turn]:
        waveform = torch.from_numpy(audio).unsqueeze(0)
        kwargs = {"num_speakers": num_speakers} if num_speakers else {}
        output = self.pipeline(
            {"waveform": waveform, "sample_rate": SAMPLE_RATE},
            hook=_Hook(progress) if progress else None,
            **kwargs,
        )
        # exclusive — без наложений: в каждый момент говорит не больше одного человека,
        # это удобно для разметки слов
        return [
            Turn(round(seg.start, 3), round(seg.end, 3), str(label))
            for seg, _, label in output.exclusive_speaker_diarization.itertracks(yield_label=True)
        ]

    @torch.inference_mode()
    def embed(self, audio: np.ndarray, spans: list[tuple[float, float]]) -> list[np.ndarray | None]:
        """Голосовой отпечаток (нормированный эмбеддинг) для каждого отрезка; None — слишком коротко."""
        model = self.pipeline._embedding
        min_len = max(int(model.min_num_samples), SAMPLE_RATE // 2)
        result: list[np.ndarray | None] = []
        for start, end in spans:
            wav = audio[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)]
            if len(wav) < min_len:
                result.append(None)
                continue
            emb = np.asarray(model(torch.from_numpy(np.ascontiguousarray(wav))[None, None]), dtype=np.float32).reshape(-1)
            norm = float(np.linalg.norm(emb))
            result.append(emb / norm if np.isfinite(norm) and norm > 0 else None)
        return result
