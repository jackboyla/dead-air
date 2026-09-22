# concurrency

6 concurrent session(s), 7 turn(s) each, 1 warmup turn(s) excluded. Started 2026-09-22T09:08:18.575267+00:00.

Host `radiance-ws`, NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02; NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02

## Outcome

| Measure | Value |
|---|---|
| Turns measured | 18 |
| Completed | 18 (100.0%) |
| Cancelled | 0 |
| Failed | 0 |
| Timed out | 0 |
| Sessions refused | 3 / 6 |
| Wall time | 53.4 s |

## Headline latency

| Measure | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| Time to first audio (server clock) | 18 | 896 ms | 945 ms | 960 ms | 960 ms | 960 ms |
| Time to first audio (user clock) | 18 | 1.06 s | 1.12 s | 1.12 s | 1.12 s | 1.12 s |
| Full response | 18 | 469 ms | 599 ms | 607 ms | 607 ms | 607 ms |

## Stage breakdown

Per-stage percentiles are computed independently, so they are not
expected to sum to the headline total — the slowest ASR turn and the
slowest TTS turn are rarely the same turn.

| Stage | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| VAD end-of-turn | 18 | 152 ms | 174 ms | 174 ms | 174 ms | 174 ms |
| ASR | 18 | 11 ms | 16 ms | 24 ms | 24 ms | 24 ms |
| LLM time to first token | 18 | 787 ms | 796 ms | 797 ms | 797 ms | 797 ms |
| TTS time to first byte | 18 | 95 ms | 147 ms | 159 ms | 159 ms | 159 ms |

## Where one median turn spends its time

A single representative turn, so the parts genuinely add up.

| Stage | Time | Share |
|---|---:|---:|
| VAD end-of-turn | 131 ms | 12% |
| ASR | 14 ms | 1% |
| LLM time to first token | 779 ms | 73% |
| TTS time to first byte | 137 ms | 13% |
| **Total** | **1.06 s** | |

## Realtime factor

Median 5.42x, worst 4.46x. Above 1.0x means speech is synthesized faster than it is spoken, which is the condition for a conversation to continue without the audio running dry.

