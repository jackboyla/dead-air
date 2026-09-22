# Fault: LLM 1.5 s slow to start

1 concurrent session(s), 6 turn(s) each, 1 warmup turn(s) excluded. Started 2026-09-22T09:11:02.564581+00:00.

**Injected faults:** `slow-start:1500ms`

Host `radiance-ws`, NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02; NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02

## Outcome

| Measure | Value |
|---|---|
| Turns measured | 5 |
| Completed | 5 (100.0%) |
| Cancelled | 0 |
| Failed | 0 |
| Timed out | 0 |
| Sessions refused | 0 / 1 |
| Wall time | 24.4 s |

## Headline latency

| Measure | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| Time to first audio (server clock) | 5 | 1.70 s | 1.71 s | 1.71 s | 1.71 s | 1.71 s |
| Time to first audio (user clock) | 5 | 1.85 s | 1.88 s | 1.88 s | 1.88 s | 1.88 s |
| Full response | 5 | 458 ms | 508 ms | 508 ms | 508 ms | 508 ms |

## Stage breakdown

Per-stage percentiles are computed independently, so they are not
expected to sum to the headline total — the slowest ASR turn and the
slowest TTS turn are rarely the same turn.

| Stage | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| VAD end-of-turn | 5 | 155 ms | 174 ms | 174 ms | 174 ms | 174 ms |
| ASR | 5 | 13 ms | 15 ms | 15 ms | 15 ms | 15 ms |
| LLM time to first token | 5 | 1.59 s | 1.60 s | 1.60 s | 1.60 s | 1.60 s |
| TTS time to first byte | 5 | 92 ms | 93 ms | 93 ms | 93 ms | 93 ms |

## Where one median turn spends its time

A single representative turn, so the parts genuinely add up.

| Stage | Time | Share |
|---|---:|---:|
| VAD end-of-turn | 152 ms | 8% |
| ASR | 12 ms | 1% |
| LLM time to first token | 1.59 s | 86% |
| TTS time to first byte | 93 ms | 5% |
| **Total** | **1.85 s** | |

## Realtime factor

Median 6.32x, worst 6.27x. Above 1.0x means speech is synthesized faster than it is spoken, which is the condition for a conversation to continue without the audio running dry.

