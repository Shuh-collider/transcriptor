"""Движки распознавания речи.

* GigaAM v3 e2e RNNT (Сбер) — русский язык, сразу с пунктуацией и таймкодами слов.
* Qwen3-ASR 1.7B (Alibaba) — 30 языков и определение языка; таймкоды слов даёт
  Qwen3-ForcedAligner.

Оба движка получают звук целиком и список фрагментов речи от VAD и распознают
фрагменты пачками на видеокарте.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterator

import numpy as np
import torch

from .media import cut
from .vad import Chunk

log = logging.getLogger(__name__)

Progress = Callable[[float], None]


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class ChunkText:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


def _batches(chunks: list[Chunk], max_items: int, max_seconds: float) -> Iterator[list[int]]:
    """Группирует фрагменты похожей длины, чтобы меньше тратить памяти на выравнивание."""
    order = sorted(range(len(chunks)), key=lambda i: chunks[i].duration, reverse=True)
    batch: list[int] = []
    seconds = 0.0
    for i in order:
        if batch and (len(batch) >= max_items or seconds + chunks[i].duration > max_seconds):
            yield batch
            batch, seconds = [], 0.0
        batch.append(i)
        seconds += chunks[i].duration
    if batch:
        yield batch


class GigaAMEngine:
    id = "gigaam"
    title = "GigaAM v3 e2e (Сбер)"
    model_name = "v3_e2e_rnnt"
    languages = ("ru",)

    def __init__(self, device: str = "cuda"):
        import gigaam

        self.model = gigaam.load_model(self.model_name, device=device)

    @torch.inference_mode()
    def transcribe(
        self, audio: np.ndarray, chunks: list[Chunk], language: str = "ru", progress: Progress | None = None
    ) -> list[ChunkText]:
        from gigaam.utils import AudioDataset

        model = self.model
        results: list[ChunkText | None] = [None] * len(chunks)
        done = 0
        for batch in _batches(chunks, max_items=32, max_seconds=480):
            wavs = [torch.from_numpy(cut(audio, chunks[i].start, chunks[i].end).copy()) for i in batch]
            wav_pad, wav_lens = AudioDataset.collate(wavs)
            wav_pad = wav_pad.to(model._device).to(model._dtype)
            wav_lens = wav_lens.to(model._device)
            encoded, encoded_len = model.forward(wav_pad, wav_lens)
            decoded = model._decode(encoded, encoded_len, wav_lens, word_timestamps=True)
            for i, (text, words) in zip(batch, decoded):
                ch = chunks[i]
                results[i] = ChunkText(
                    ch.start,
                    ch.end,
                    text.strip(),
                    [Word(w.text, round(ch.start + w.start, 3), round(ch.start + w.end, 3)) for w in words or []],
                )
            done += len(batch)
            if progress:
                progress(done / len(chunks))
        return [r for r in results if r is not None]


class QwenEngine:
    id = "qwen"
    title = "Qwen3-ASR 1.7B (Alibaba)"
    asr_repo = "Qwen/Qwen3-ASR-1.7B-hf"
    aligner_repo = "Qwen/Qwen3-ForcedAligner-0.6B-hf"

    def __init__(self, device: str = "cuda"):
        from transformers import AutoModelForMultimodalLM, AutoProcessor
        from transformers.models.qwen3_asr import processing_qwen3_asr as qp

        self._qp = qp
        self.device = device
        self.processor = AutoProcessor.from_pretrained(self.asr_repo)
        self.model = AutoModelForMultimodalLM.from_pretrained(
            self.asr_repo, dtype=torch.bfloat16, device_map=device
        ).eval()
        self.languages = tuple(qp.LANGUAGE_CODE_TO_NAME)
        self._name_to_code = {name.lower(): code for code, name in qp.LANGUAGE_CODE_TO_NAME.items()}
        self._aligner = None
        self._aligner_processor = None

    def _generate(self, arrays: list[np.ndarray], language: str | None, max_new_tokens: int) -> list[dict]:
        inputs = self.processor.apply_transcription_request(
            audio=arrays, language=[language] * len(arrays) if language else None
        ).to(self.model.device, self.model.dtype)
        out = self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        parsed = self.processor.decode(out[:, inputs["input_ids"].shape[1] :], return_format="parsed")
        return parsed if isinstance(parsed, list) else [parsed]

    @torch.inference_mode()
    def detect_language(self, audio: np.ndarray, chunks: list[Chunk], samples: int = 5) -> str | None:
        """Определяет язык по нескольким фрагментам из разных частей видео (голосованием)."""
        if not chunks:
            return None
        step = max(1, len(chunks) // samples)
        picked = [chunks[i] for i in range(0, len(chunks), step)][:samples]
        arrays = [cut(audio, c.start, c.end) for c in picked]
        votes = Counter()
        for item in self._generate(arrays, None, max_new_tokens=24):
            name = (item.get("language") or "").strip().lower()
            if name in self._name_to_code:
                votes[self._name_to_code[name]] += 1
        return votes.most_common(1)[0][0] if votes else None

    @torch.inference_mode()
    def transcribe(
        self, audio: np.ndarray, chunks: list[Chunk], language: str | None = None, progress: Progress | None = None
    ) -> list[ChunkText]:
        results: list[ChunkText | None] = [None] * len(chunks)
        done = 0
        for batch in _batches(chunks, max_items=12, max_seconds=240):
            arrays = [cut(audio, chunks[i].start, chunks[i].end) for i in batch]
            longest = max(chunks[i].duration for i in batch)
            parsed = self._generate(arrays, language, max_new_tokens=int(longest * 16) + 32)
            texts = [p.get("transcription", "").strip() for p in parsed]
            aligned = self._align(arrays, texts, language)
            for i, text, words in zip(batch, texts, aligned):
                ch = chunks[i]
                shifted = [Word(w.text, round(ch.start + w.start, 3), round(ch.start + w.end, 3)) for w in words]
                results[i] = ChunkText(ch.start, ch.end, text, shifted)
            done += len(batch)
            if progress:
                progress(done / len(chunks))
        return [r for r in results if r is not None]

    def _align(self, arrays: list[np.ndarray], texts: list[str], language: str | None) -> list[list[Word]]:
        name = self._qp.LANGUAGE_CODE_TO_NAME.get(language or "")
        if name not in self._qp.FORCED_ALIGNER_LANGUAGES:
            return [[] for _ in texts]
        if self._aligner is None:
            from transformers import AutoModelForTokenClassification, AutoProcessor

            self._aligner_processor = AutoProcessor.from_pretrained(self.aligner_repo)
            self._aligner = AutoModelForTokenClassification.from_pretrained(
                self.aligner_repo, dtype=torch.bfloat16, device_map=self.device
            ).eval()
        proc, aligner = self._aligner_processor, self._aligner

        out: list[list[Word]] = [[] for _ in texts]
        todo = [k for k, t in enumerate(texts) if proc.split_words_for_alignment(t, name)]
        if not todo:
            return out
        inputs, word_lists = proc.prepare_forced_aligner_inputs(
            audio=[arrays[k] for k in todo], transcript=[texts[k] for k in todo], language=name
        )
        inputs = inputs.to(aligner.device, aligner.dtype)
        logits = aligner(**inputs).logits
        stamps = proc.decode_forced_alignment(
            logits=logits,
            input_ids=inputs["input_ids"],
            word_lists=word_lists,
            timestamp_token_id=aligner.config.timestamp_token_id,
        )
        for k, items in zip(todo, stamps):
            out[k] = _restore_punctuation(texts[k], items, lambda tok: proc.split_words_for_alignment(tok, name))
        return out


def _restore_punctuation(text: str, aligned: list[dict], split: Callable[[str], list[str]]) -> list[Word]:
    """Выравниватель теряет пунктуацию: переносим таймкоды обратно на слова исходного текста."""
    words: list[Word] = []
    prefix = ""
    pos = 0
    for token in text.split():
        n = len(split(token))
        if n == 0 or pos >= len(aligned):
            if words:
                words[-1].text += " " + token
            else:
                prefix += token + " "
            continue
        part = aligned[pos : pos + n]
        pos += n
        words.append(Word(prefix + token, part[0]["start_time"], part[-1]["end_time"]))
        prefix = ""
    return words


ENGINES = {GigaAMEngine.id: GigaAMEngine, QwenEngine.id: QwenEngine}
