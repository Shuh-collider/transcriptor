"""Декодирование звука через ffmpeg в 16 кГц моно."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg
import numpy as np

SAMPLE_RATE = 16_000


def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def _no_window() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def load_audio(path: Path | str) -> np.ndarray:
    """Возвращает звук файла как float32 моно 16 кГц."""
    cmd = [
        ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "0",
        "-i", str(path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "-acodec", "pcm_s16le", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, creationflags=_no_window())
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise RuntimeError(f"ffmpeg не смог прочитать {Path(path).name}: {err}")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def cut(audio: np.ndarray, start: float, end: float) -> np.ndarray:
    return audio[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)]
