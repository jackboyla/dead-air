#!/usr/bin/env python3
"""Build prompts that pause mid-sentence, to measure false endpointing.

``speculative_reopen_ms`` is the largest single term in this stack's latency
budget, and it is tempting to just turn it down. This generates the evidence for
what that costs.

Each prompt is one question split across a silent gap, the way people actually
talk: "What is the capital ... of France?". A pipeline that commits a turn during
the gap answers a fragment and talks over the rest of the sentence. A pipeline
that waits answers the whole question. The reopen window is exactly the setting
that decides which happens, so the gap lengths bracket the values worth testing.

Usage::

    ../speech-to-speech/.venv/bin/python scripts/make_hesitation_prompts.py \\
        --out assets/prompts-hesitation
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

# Reuse the synthesis path from the main prompt builder so both sets share a voice.
from make_prompts import TARGET_RATE_HZ, _trim_silence, _write_wav, synthesize

# (name, first half, gap in ms, second half). Gaps bracket the default 800 ms
# reopen window from well below to just above it.
HESITATIONS: list[tuple[str, str, int, str]] = [
    ("capital_300", "What is the capital", 300, "of France?"),
    ("boil_500", "At what temperature does water", 500, "boil at sea level?"),
    ("planets_700", "How many planets are", 700, "in the solar system?"),
    ("mountain_900", "What is the tallest mountain", 900, "in the world?"),
    ("ocean_400", "Which ocean is", 400, "the largest?"),
    ("moon_600", "In what year did humans", 600, "first land on the moon?"),
]


def build(name: str, head: str, gap_ms: int, tail: str, voice: str, rate: int) -> tuple[np.ndarray, str]:
    head_audio = _trim_silence(synthesize(head, voice, rate), rate=rate)
    tail_audio = _trim_silence(synthesize(tail, voice, rate), rate=rate)
    gap = np.zeros(int(rate * gap_ms / 1000), dtype=np.float32)
    return np.concatenate([head_audio, gap, tail_audio]), f"{head} {tail}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("assets/prompts-hesitation"))
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--rate", type=int, default=TARGET_RATE_HZ)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    entries = []
    for name, head, gap_ms, tail in HESITATIONS:
        wav_path = args.out / f"{name}.wav"
        if wav_path.exists() and not args.force:
            print(f"  skip  {wav_path}")
            text = f"{head} {tail}"
        else:
            samples, text = build(name, head, gap_ms, tail, args.voice, args.rate)
            _write_wav(wav_path, samples, args.rate)
            print(f"  write {wav_path}  {len(samples) / args.rate:.2f}s  gap={gap_ms}ms")
        entries.append(
            {
                "name": name,
                "file": wav_path.name,
                "text": text,
                "gap_ms": gap_ms,
                "sha256_16": hashlib.sha256(wav_path.read_bytes()).hexdigest()[:16],
            }
        )

    manifest = {
        "voice": args.voice,
        "rate": args.rate,
        "generator": "kokoro",
        "note": "Each prompt is one question split by a silent gap, for measuring false endpointing.",
        "prompts": entries,
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {args.out / 'manifest.json'} with {len(entries)} prompts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
