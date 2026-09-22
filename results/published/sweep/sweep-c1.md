# concurrency

1 concurrent session(s), 7 turn(s) each, 1 warmup turn(s) excluded. Started 2026-09-22T09:07:08.888741+00:00.

Host `radiance-ws`, NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02; NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02

## Outcome

| Measure | Value |
|---|---|
| Turns measured | 6 |
| Completed | 6 (100.0%) |
| Cancelled | 0 |
| Failed | 0 |
| Timed out | 0 |
| Sessions refused | 0 / 1 |
| Wall time | 23.0 s |

## Headline latency

| Measure | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| Time to first audio (server clock) | 6 | 892 ms | 899 ms | 899 ms | 899 ms | 899 ms |
| Time to first audio (user clock) | 6 | 1.05 s | 1.07 s | 1.07 s | 1.07 s | 1.07 s |
| Full response | 6 | 406 ms | 592 ms | 592 ms | 592 ms | 592 ms |

## Stage breakdown

Per-stage percentiles are computed independently, so they are not
expected to sum to the headline total — the slowest ASR turn and the
slowest TTS turn are rarely the same turn.

| Stage | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| VAD end-of-turn | 6 | 155 ms | 174 ms | 174 ms | 174 ms | 174 ms |
| ASR | 6 | 15 ms | 24 ms | 24 ms | 24 ms | 24 ms |
| LLM time to first token | 6 | 783 ms | 792 ms | 792 ms | 792 ms | 792 ms |
| TTS time to first byte | 6 | 92 ms | 102 ms | 102 ms | 102 ms | 102 ms |

## Where one median turn spends its time

A single representative turn, so the parts genuinely add up.

| Stage | Time | Share |
|---|---:|---:|
| VAD end-of-turn | 150 ms | 14% |
| ASR | 24 ms | 2% |
| LLM time to first token | 783 ms | 75% |
| TTS time to first byte | 91 ms | 9% |
| **Total** | **1.05 s** | |

## Realtime factor

Median 6.24x, worst 6.06x. Above 1.0x means speech is synthesized faster than it is spoken, which is the condition for a conversation to continue without the audio running dry.

