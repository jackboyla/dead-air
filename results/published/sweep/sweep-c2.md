# concurrency

2 concurrent session(s), 7 turn(s) each, 1 warmup turn(s) excluded. Started 2026-09-22T09:07:31.938144+00:00.

Host `radiance-ws`, NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02; NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02

## Outcome

| Measure | Value |
|---|---|
| Turns measured | 12 |
| Completed | 12 (100.0%) |
| Cancelled | 0 |
| Failed | 0 |
| Timed out | 0 |
| Sessions refused | 0 / 2 |
| Wall time | 23.0 s |

## Headline latency

| Measure | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| Time to first audio (server clock) | 12 | 897 ms | 915 ms | 928 ms | 928 ms | 928 ms |
| Time to first audio (user clock) | 12 | 1.05 s | 1.07 s | 1.10 s | 1.10 s | 1.10 s |
| Full response | 12 | 436 ms | 627 ms | 735 ms | 735 ms | 735 ms |

## Stage breakdown

Per-stage percentiles are computed independently, so they are not
expected to sum to the headline total — the slowest ASR turn and the
slowest TTS turn are rarely the same turn.

| Stage | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| VAD end-of-turn | 12 | 150 ms | 171 ms | 175 ms | 175 ms | 175 ms |
| ASR | 12 | 24 ms | 27 ms | 28 ms | 28 ms | 28 ms |
| LLM time to first token | 12 | 778 ms | 789 ms | 790 ms | 790 ms | 790 ms |
| TTS time to first byte | 12 | 95 ms | 116 ms | 124 ms | 124 ms | 124 ms |

## Where one median turn spends its time

A single representative turn, so the parts genuinely add up.

| Stage | Time | Share |
|---|---:|---:|
| VAD end-of-turn | 149 ms | 14% |
| ASR | 24 ms | 2% |
| LLM time to first token | 776 ms | 74% |
| TTS time to first byte | 105 ms | 10% |
| **Total** | **1.05 s** | |

## Realtime factor

Median 6.15x, worst 4.63x. Above 1.0x means speech is synthesized faster than it is spoken, which is the condition for a conversation to continue without the audio running dry.

