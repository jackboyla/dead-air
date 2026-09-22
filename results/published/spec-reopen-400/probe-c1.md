# Variant: speculative_reopen_ms=400

1 concurrent session(s), 9 turn(s) each, 1 warmup turn(s) excluded. Started 2026-09-22T09:04:46.635078+00:00.

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
| Wall time | 25.6 s |

## Headline latency

| Measure | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| Time to first audio (server clock) | 8 | 491 ms | 499 ms | 499 ms | 499 ms | 499 ms |
| Time to first audio (user clock) | 8 | 646 ms | 664 ms | 664 ms | 664 ms | 664 ms |
| Full response | 8 | 398 ms | 659 ms | 659 ms | 659 ms | 659 ms |

## Stage breakdown

Per-stage percentiles are computed independently, so they are not
expected to sum to the headline total — the slowest ASR turn and the
slowest TTS turn are rarely the same turn.

| Stage | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| VAD end-of-turn | 8 | 151 ms | 173 ms | 173 ms | 173 ms | 173 ms |
| ASR | 8 | 23 ms | 26 ms | 26 ms | 26 ms | 26 ms |
| LLM time to first token | 8 | 380 ms | 390 ms | 390 ms | 390 ms | 390 ms |
| TTS time to first byte | 8 | 94 ms | 102 ms | 102 ms | 102 ms | 102 ms |

## Where one median turn spends its time

A single representative turn, so the parts genuinely add up.

| Stage | Time | Share |
|---|---:|---:|
| VAD end-of-turn | 148 ms | 23% |
| ASR | 24 ms | 4% |
| LLM time to first token | 379 ms | 59% |
| TTS time to first byte | 95 ms | 15% |
| **Total** | **646 ms** | |

## Realtime factor

Median 6.10x, worst 6.04x. Above 1.0x means speech is synthesized faster than it is spoken, which is the condition for a conversation to continue without the audio running dry.

