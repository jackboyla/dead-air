# concurrency

4 concurrent session(s), 7 turn(s) each, 1 warmup turn(s) excluded. Started 2026-09-22T09:07:55.001034+00:00.

Host `radiance-ws`, NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02; NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02

## Outcome

| Measure | Value |
|---|---|
| Turns measured | 18 |
| Completed | 18 (100.0%) |
| Cancelled | 0 |
| Failed | 0 |
| Timed out | 0 |
| Sessions refused | 1 / 4 |
| Wall time | 23.6 s |

## Headline latency

| Measure | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| Time to first audio (server clock) | 18 | 900 ms | 953 ms | 953 ms | 953 ms | 953 ms |
| Time to first audio (user clock) | 18 | 1.07 s | 1.10 s | 1.10 s | 1.10 s | 1.10 s |
| Full response | 18 | 483 ms | 570 ms | 803 ms | 803 ms | 803 ms |

## Stage breakdown

Per-stage percentiles are computed independently, so they are not
expected to sum to the headline total — the slowest ASR turn and the
slowest TTS turn are rarely the same turn.

| Stage | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| VAD end-of-turn | 18 | 151 ms | 170 ms | 172 ms | 172 ms | 172 ms |
| ASR | 18 | 15 ms | 25 ms | 26 ms | 26 ms | 26 ms |
| LLM time to first token | 18 | 785 ms | 792 ms | 793 ms | 793 ms | 793 ms |
| TTS time to first byte | 18 | 96 ms | 144 ms | 146 ms | 146 ms | 146 ms |

## Where one median turn spends its time

A single representative turn, so the parts genuinely add up.

| Stage | Time | Share |
|---|---:|---:|
| VAD end-of-turn | 168 ms | 16% |
| ASR | 15 ms | 1% |
| LLM time to first token | 793 ms | 74% |
| TTS time to first byte | 92 ms | 9% |
| **Total** | **1.07 s** | |

## Realtime factor

Median 5.31x, worst 4.02x. Above 1.0x means speech is synthesized faster than it is spoken, which is the condition for a conversation to continue without the audio running dry.

