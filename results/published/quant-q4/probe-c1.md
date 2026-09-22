# Quantisation: Q4_0

1 concurrent session(s), 9 turn(s) each, 1 warmup turn(s) excluded. Started 2026-09-22T09:17:07.349890+00:00.

Identical to the Q8_0 run except the LLM is gemma-4-E4B-it Q4_0.

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
| Wall time | 29.2 s |

## Headline latency

| Measure | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| Time to first audio (server clock) | 8 | 894 ms | 900 ms | 900 ms | 900 ms | 900 ms |
| Time to first audio (user clock) | 8 | 1.05 s | 1.07 s | 1.07 s | 1.07 s | 1.07 s |
| Full response | 8 | 406 ms | 529 ms | 529 ms | 529 ms | 529 ms |

## Stage breakdown

Per-stage percentiles are computed independently, so they are not
expected to sum to the headline total — the slowest ASR turn and the
slowest TTS turn are rarely the same turn.

| Stage | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| VAD end-of-turn | 8 | 152 ms | 173 ms | 173 ms | 173 ms | 173 ms |
| ASR | 8 | 15 ms | 25 ms | 25 ms | 25 ms | 25 ms |
| LLM time to first token | 8 | 785 ms | 791 ms | 791 ms | 791 ms | 791 ms |
| TTS time to first byte | 8 | 92 ms | 102 ms | 102 ms | 102 ms | 102 ms |

## Where one median turn spends its time

A single representative turn, so the parts genuinely add up.

| Stage | Time | Share |
|---|---:|---:|
| VAD end-of-turn | 152 ms | 15% |
| ASR | 16 ms | 1% |
| LLM time to first token | 787 ms | 75% |
| TTS time to first byte | 91 ms | 9% |
| **Total** | **1.05 s** | |

## Realtime factor

Median 6.28x, worst 5.98x. Above 1.0x means speech is synthesized faster than it is spoken, which is the condition for a conversation to continue without the audio running dry.

