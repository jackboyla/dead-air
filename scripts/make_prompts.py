#!/usr/bin/env python3
"""Generate the prompt audio the probe speaks at the server.

Run once. The WAVs are cached on disk and deliberately not committed.

Synthesized speech is used rather than recordings of people, which is a real
limitation and worth stating plainly: TTS audio is cleaner than a microphone in a
room, so measured ASR latency here is a lower bound on what a person would see.
It buys reproducibility — anyone can regenerate byte-similar prompts from the
pinned text below without shipping audio or chasing a dataset licence.

Kokoro is used here rather than the deployment's own Qwen3-TTS so the measurement
input does not come from a component under test.

Usage::

    # From the speech-to-speech virtualenv, which already has kokoro:
    ../speech-to-speech/.venv/bin/python scripts/make_prompts.py --out assets/prompts

    # Or point at any environment with kokoro installed:
    python scripts/make_prompts.py --out assets/prompts --voice af_heart
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import wave
from pathlib import Path

import numpy as np

TARGET_RATE_HZ = 16_000

# Short, unambiguous questions with short correct answers. Chosen so that a reply
# is a sentence rather than an essay: a long answer measures the synthesizer's
# throughput, while this benchmark is about how fast the first word arrives.
# Each is lexically distinct so a transcript leaking between concurrent sessions
# is obvious on sight.
PROMPTS: list[tuple[str, str]] = [
    ("capital_france", "What is the capital of France?"),
    ("water_boil", "At what temperature does water boil at sea level?"),
    ("planet_count", "How many planets are in the solar system?"),
    ("tallest_mountain", "What is the tallest mountain in the world?"),
    ("ocean_largest", "Which ocean is the largest?"),
    ("light_speed", "Roughly how fast does light travel?"),
    ("moon_landing", "In what year did humans first land on the moon?"),
    ("bee_product", "What do bees make besides honey?"),
    ("triangle_angles", "How many degrees are in a triangle?"),
    ("continent_count", "How many continents are there?"),
    ("dna_stands", "What does DNA stand for?"),
    ("red_blue_mix", "What colour do you get when you mix red and blue?"),
]


def synthesize(text: str, voice: str, rate: int) -> np.ndarray:
    """Render one prompt to mono float32 at ``rate`` Hz."""

    try:
        import kokoro  # noqa: F401  - imported to check availability, used via _pipeline
    except ImportError:  # pragma: no cover - environment guidance, not logic
        print(
            "error: kokoro is not installed in this interpreter.\n"
            "Run this script from an environment that has it, for example:\n"
            "  ../speech-to-speech/.venv/bin/python scripts/make_prompts.py",
            file=sys.stderr,
        )
        raise SystemExit(2) from None

    pipeline = _pipeline(voice)
    chunks = [audio for _, _, audio in pipeline(text, voice=voice)]
    if not chunks:
        raise RuntimeError(f"kokoro produced no audio for {text!r}")
    samples = np.concatenate([np.asarray(chunk, dtype=np.float32).reshape(-1) for chunk in chunks])
    return _resample(samples, _KOKORO_RATE_HZ, rate)


_KOKORO_RATE_HZ = 24_000
_PIPELINE_CACHE: dict[str, object] = {}


def _pipeline(voice: str):
    """Build the Kokoro pipeline once; loading it per prompt dominates runtime."""

    from kokoro import KPipeline

    # Kokoro selects its grapheme-to-phoneme frontend from the voice's first
    # letter: 'a' for American English, 'b' for British, and so on.
    code = voice[0] if voice else "a"
    if code not in _PIPELINE_CACHE:
        _PIPELINE_CACHE[code] = KPipeline(lang_code=code)
    return _PIPELINE_CACHE[code]


def _resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return samples
    try:
        import soxr

        return np.asarray(soxr.resample(samples, source_rate, target_rate), dtype=np.float32)
    except ImportError:
        duration = len(samples) / source_rate
        index = np.linspace(0.0, len(samples) - 1, num=int(round(duration * target_rate)), dtype=np.float64)
        return np.interp(index, np.arange(len(samples)), samples).astype(np.float32)


def _trim_silence(
    samples: np.ndarray, threshold: float = 0.01, pad_ms: int = 40, rate: int = TARGET_RATE_HZ
) -> np.ndarray:
    """Trim leading and trailing near-silence, keeping a small pad.

    Leading silence would be charged to the pipeline's VAD as if the speaker had
    paused, inflating every latency measured from the start of the utterance.
    """

    loud = np.flatnonzero(np.abs(samples) > threshold)
    if loud.size == 0:
        return samples
    pad = int(rate * pad_ms / 1000)
    start = max(0, int(loud[0]) - pad)
    end = min(len(samples), int(loud[-1]) + pad)
    return samples[start:end]


def _write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("assets/prompts"))
    parser.add_argument("--voice", default="af_heart", help="Kokoro voice. Default is af_heart.")
    parser.add_argument("--rate", type=int, default=TARGET_RATE_HZ, help="Output sample rate.")
    parser.add_argument("--force", action="store_true", help="Regenerate prompts that already exist.")
    args = parser.parse_args()

    entries = []
    for name, text in PROMPTS:
        wav_path = args.out / f"{name}.wav"
        if wav_path.exists() and not args.force:
            print(f"  skip  {wav_path}")
        else:
            samples = _trim_silence(synthesize(text, args.voice, args.rate), rate=args.rate)
            _write_wav(wav_path, samples, args.rate)
            print(f"  write {wav_path}  {len(samples) / args.rate:.2f}s")
        digest = hashlib.sha256(wav_path.read_bytes()).hexdigest()[:16]
        entries.append({"name": name, "file": wav_path.name, "text": text, "sha256_16": digest})

    manifest = {
        "voice": args.voice,
        "rate": args.rate,
        "generator": "kokoro",
        "note": (
            "Synthesized prompts. Cleaner than microphone speech, so measured ASR "
            "latency is a lower bound on what real users would see."
        ),
        "prompts": entries,
    }
    manifest_path = args.out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {manifest_path} with {len(entries)} prompts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
