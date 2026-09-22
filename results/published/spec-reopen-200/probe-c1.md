# Variant: speculative_reopen_ms=200

1 concurrent session(s), 9 turn(s) each, 1 warmup turn(s) excluded. Started 2026-09-22T08:59:23.870222+00:00.

Identical to baseline except the speculative turn reopen window is cut from 800 ms to 200 ms.

Host `radiance-ws`, NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02; NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02

## Outcome

| Measure | Value |
|---|---|
| Turns measured | 8 |
| Completed | 8 (100.0%) |
| Cancelled | 0 |
| Failed | 0 |
| Timed out | 0 |
| Sessions refused | 0 / 1 |
| Wall time | 24.2 s |

## Headline latency

| Measure | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| Time to first audio (server clock) | 8 | 296 ms | 300 ms | 300 ms | 300 ms | 300 ms |
| Time to first audio (user clock) | 8 | 444 ms | 467 ms | 467 ms | 467 ms | 467 ms |
| Full response | 8 | 382 ms | 489 ms | 489 ms | 489 ms | 489 ms |

## Stage breakdown

Per-stage percentiles are computed independently, so they are not
expected to sum to the headline total — the slowest ASR turn and the
slowest TTS turn are rarely the same turn.

| Stage | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| VAD end-of-turn | 8 | 149 ms | 173 ms | 173 ms | 173 ms | 173 ms |
| ASR | 8 | 16 ms | 28 ms | 28 ms | 28 ms | 28 ms |
| LLM time to first token | 8 | 182 ms | 195 ms | 195 ms | 195 ms | 195 ms |
| TTS time to first byte | 8 | 94 ms | 95 ms | 95 ms | 95 ms | 95 ms |

## Where one median turn spends its time

A single representative turn, so the parts genuinely add up.

| Stage | Time | Share |
|---|---:|---:|
| VAD end-of-turn | 149 ms | 33% |
| ASR | 24 ms | 5% |
| LLM time to first token | 178 ms | 40% |
| TTS time to first byte | 94 ms | 21% |
| **Total** | **444 ms** | |

## Realtime factor

Median 6.20x, worst 6.05x. Above 1.0x means speech is synthesized faster than it is spoken, which is the condition for a conversation to continue without the audio running dry.

