"""Write a prompt directory of tone bursts, for driving ``deadair mock``.

Tones carry no words, so they are useless against the real pipeline, whose VAD
and ASR need speech. The mock only gates on energy, and this needs no TTS model,
which is what CI wants.

    uv run python scripts/make_tone_prompts.py --out /tmp/tones
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from deadair.probe.audio import PIPELINE_RATE_HZ, write_wav


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--count", type=int, default=4)
    parser.add_argument("--seconds", type=float, default=0.6)
    args = parser.parse_args()

    entries = []
    t = np.arange(int(args.seconds * PIPELINE_RATE_HZ)) / PIPELINE_RATE_HZ
    for index in range(args.count):
        name = f"tone_{index}"
        write_wav(args.out / f"{name}.wav", 0.3 * np.sin(2 * np.pi * (220 + 110 * index) * t), PIPELINE_RATE_HZ)
        entries.append({"name": name, "file": f"{name}.wav", "text": f"<tone {index}>"})
    manifest = {"rate": PIPELINE_RATE_HZ, "generator": "tone", "prompts": entries}
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.count} tone prompts to {args.out}")


if __name__ == "__main__":
    main()
