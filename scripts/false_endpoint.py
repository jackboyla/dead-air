#!/usr/bin/env python3
"""Measure how often the pipeline answers before the speaker has finished.

Run this against a server, change ``--speculative_reopen_ms``, run it again. The
pair of numbers is the trade that ``docs/latency-budget.md`` is about: a shorter
reopen window is faster and interrupts more.

The measure is deliberately blunt, because the failure is blunt. For each prompt
that contains one mid-sentence pause, count how many responses the server opened:

* one response, transcript containing the whole question -> the pause was absorbed
* two responses, or a transcript that stops at the pause -> the speaker was cut off

Usage::

    uv run python scripts/false_endpoint.py --prompts assets/prompts-hesitation
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from deadair.probe.audio import PromptLibrary, silence
from deadair.probe.client import RealtimeProbe, SessionConfig

SETTLE_S = 2.5


async def measure(url: str, prompts: PromptLibrary, repeats: int) -> list[dict]:
    """Speak every prompt down one connection, scoring each in isolation.

    One connection rather than one per prompt: a released session does not free its
    pipeline slot instantly, so reconnecting per prompt races the server's own drain
    and earns ``session_limit_reached`` instead of a measurement. Speaking down a
    single connection is also what a real conversation does.
    """

    rows: list[dict] = []
    probe = RealtimeProbe(SessionConfig(url=url), session_index=0)
    async with probe.connect():
        for repeat in range(repeats):
            for prompt in prompts:
                before = len(probe.builder.turns)
                probe.builder.mark_client_speech_start(time.monotonic(), prompt=prompt.text)
                await probe._stream_pcm(prompt.pcm)
                probe.builder.mark_client_speech_end(time.monotonic())
                # Let anything the server started during the gap finish arriving,
                # rather than moving on and losing the evidence.
                await probe._stream_pcm(silence(SETTLE_S, probe.config.send_rate))
                await asyncio.sleep(0.5)

                produced = probe.builder.turns[before:]
                responses = [t for t in produced if t.response_id is not None]
                transcripts = [t.transcript for t in produced if t.transcript]
                whole = any(_covers(text, prompt.text) for text in transcripts)
                rows.append(
                    {
                        "repeat": repeat,
                        "prompt": prompt.name,
                        "text": prompt.text,
                        "responses": len(responses),
                        "transcripts": transcripts,
                        "interrupted": len(responses) > 1 or not whole,
                    }
                )
                label = "CUT" if rows[-1]["interrupted"] else "ok "
                print(f"  {label} {prompt.name:18} responses={len(responses)} transcripts={transcripts}")
    return rows


def _covers(transcript: str, expected: str) -> bool:
    """True when a transcript contains the tail of the expected question.

    Compares the last three words rather than the whole string: ASR punctuation
    and casing vary run to run, but a turn cut off at the pause is missing the end
    of the sentence entirely, which those three words detect.
    """

    tail = [word.strip(".,?!").lower() for word in expected.split()[-3:]]
    got = [word.strip(".,?!").lower() for word in transcript.split()]
    return all(word in got for word in tail)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="ws://127.0.0.1:18765/v1/realtime")
    parser.add_argument("--prompts", type=Path, default=Path("assets/prompts-hesitation"))
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--label", default="run", help="Name recorded with the results.")
    parser.add_argument("--out", type=Path, default=Path("results/false-endpoint"))
    args = parser.parse_args()

    prompts = PromptLibrary.load(args.prompts)
    print(f"Speaking {len(prompts)} hesitation prompts x{args.repeats} at {args.url}")
    rows = asyncio.run(measure(args.url, prompts, args.repeats))

    interrupted = sum(1 for row in rows if row["interrupted"])
    summary = {
        "label": args.label,
        "url": args.url,
        "prompts": len(prompts),
        "trials": len(rows),
        "interrupted": interrupted,
        "interruption_rate": round(interrupted / len(rows), 4) if rows else 0.0,
        "rows": rows,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{args.label}.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(
        f"\n{interrupted}/{len(rows)} trials interrupted the speaker ({summary['interruption_rate']:.0%}). Wrote {path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
