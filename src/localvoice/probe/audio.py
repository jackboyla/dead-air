"""Prompt audio handling for the probe.

The probe drives the pipeline with real speech rather than synthetic tones,
because the stages being measured are speech stages: server-side VAD has to decide
the utterance ended, and ASR has to produce a transcript the language model can
answer. A sine wave exercises neither, and a silence-only prompt measures nothing
but the VAD's timeout.

Prompt audio is generated once by ``scripts/make_prompts.py`` using the same local
TTS the deployment runs, then cached on disk. It is deliberately not committed:
audio regenerated from the pinned prompt texts is reproducible enough for latency
work, and a repository is a bad place to keep a few megabytes of WAV.
"""

from __future__ import annotations

import hashlib
import json
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

PIPELINE_RATE_HZ = 16_000
BYTES_PER_SAMPLE = 2


@dataclass(frozen=True)
class Prompt:
    """One utterance the probe can speak at the server."""

    name: str
    text: str
    pcm: bytes
    rate: int

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / BYTES_PER_SAMPLE / self.rate

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.pcm).hexdigest()[:16]


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Read a WAV into float32 in [-1, 1], downmixing to mono."""

    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != BYTES_PER_SAMPLE:
            raise ValueError(f"{path}: expected 16-bit PCM, got {handle.getsampwidth() * 8}-bit")
        channels = handle.getnchannels()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())

    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(BYTES_PER_SAMPLE)
        handle.setframerate(rate)
        handle.writeframes(pcm)


def resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Resample mono float audio, preferring soxr when it is installed.

    soxr is what the pipeline itself uses. Falling back to linear interpolation
    keeps the probe usable without it, at some cost in high-frequency accuracy —
    which is why the fallback warns rather than passing silently.
    """

    if source_rate == target_rate:
        return samples
    try:
        import soxr

        return np.asarray(soxr.resample(samples, source_rate, target_rate), dtype=np.float32)
    except ImportError:
        import warnings

        warnings.warn(
            f"soxr is not installed; falling back to linear resampling {source_rate} -> {target_rate}. "
            "Transcription accuracy may differ from the deployment's own resampling.",
            RuntimeWarning,
            stacklevel=2,
        )
        duration = len(samples) / source_rate
        target_length = int(round(duration * target_rate))
        source_index = np.linspace(0.0, len(samples) - 1, num=target_length, dtype=np.float64)
        return np.interp(source_index, np.arange(len(samples)), samples).astype(np.float32)


def to_pcm16(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def silence(duration_s: float, rate: int = PIPELINE_RATE_HZ) -> bytes:
    return b"\x00\x00" * int(duration_s * rate)


def chunk_pcm(pcm: bytes, chunk_ms: int, rate: int) -> list[bytes]:
    """Split PCM into fixed-duration frames, as a microphone would deliver them."""

    size = max(BYTES_PER_SAMPLE, int(rate * BYTES_PER_SAMPLE * chunk_ms / 1000))
    size -= size % BYTES_PER_SAMPLE
    return [pcm[offset : offset + size] for offset in range(0, len(pcm), size)]


class PromptLibrary:
    """Prompt audio loaded from a directory produced by ``scripts/make_prompts.py``.

    The directory holds one WAV per prompt plus a ``manifest.json`` recording the
    text, the voice, and the sample rate each was generated at, so a published
    result can name exactly what was spoken at the server.
    """

    def __init__(self, prompts: list[Prompt]) -> None:
        if not prompts:
            raise ValueError("prompt library is empty")
        self.prompts = prompts

    def __len__(self) -> int:
        return len(self.prompts)

    def __iter__(self):
        return iter(self.prompts)

    def get(self, index: int) -> Prompt:
        """Prompts cycle, so a run may request more turns than there are prompts."""

        return self.prompts[index % len(self.prompts)]

    def for_session(self, session_index: int, turn_index: int) -> Prompt:
        """Pick a prompt that differs across concurrent sessions at the same turn.

        Each session is offset by one prompt, so at any turn every session is saying
        something different as long as there are at least as many prompts as
        sessions. If audio or text ever leaks between sessions it shows up as a
        mismatched transcript rather than hiding behind everyone saying the same
        sentence.
        """

        return self.get(turn_index + session_index)

    @classmethod
    def load(cls, directory: Path, target_rate: int = PIPELINE_RATE_HZ) -> "PromptLibrary":
        manifest_path = directory / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(
                f"No prompt manifest at {manifest_path}. Generate prompts first:\n"
                f"  python scripts/make_prompts.py --out {directory}"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        prompts: list[Prompt] = []
        for entry in manifest["prompts"]:
            wav_path = directory / entry["file"]
            samples, rate = read_wav(wav_path)
            samples = resample(samples, rate, target_rate)
            prompts.append(
                Prompt(
                    name=entry.get("name", wav_path.stem),
                    text=entry["text"],
                    pcm=to_pcm16(samples),
                    rate=target_rate,
                )
            )
        return cls(prompts)
