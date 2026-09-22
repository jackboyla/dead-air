# Fault: LLM returns 503 half the time

1 concurrent session(s), 7 turn(s) each, 1 warmup turn(s) excluded. Started 2026-09-22T09:11:53.347036+00:00.

**Injected faults:** `reject:503;p=0.5`

Host `radiance-ws`, NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02; NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02

## Outcome

| Measure | Value |
|---|---|
| Turns measured | 6 |
| Completed | 4 (66.7%) |
| Cancelled | 0 |
| Failed | 2 |
| Timed out | 0 |
| Sessions refused | 0 / 1 |
| Wall time | 24.4 s |

## Headline latency

| Measure | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| Time to first audio (server clock) | 6 | 893 ms | 1.44 s | 1.44 s | 1.44 s | 1.44 s |
| Time to first audio (user clock) | 6 | 1.06 s | 1.61 s | 1.61 s | 1.61 s | 1.61 s |
| Full response | 6 | 425 ms | 467 ms | 467 ms | 467 ms | 467 ms |

## Stage breakdown

Per-stage percentiles are computed independently, so they are not
expected to sum to the headline total — the slowest ASR turn and the
slowest TTS turn are rarely the same turn.

| Stage | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| VAD end-of-turn | 6 | 153 ms | 172 ms | 172 ms | 172 ms | 172 ms |
| ASR | 6 | 14 ms | 25 ms | 25 ms | 25 ms | 25 ms |
| LLM time to first token | 6 | 791 ms | 1.34 s | 1.34 s | 1.34 s | 1.34 s |
| TTS time to first byte | 6 | 92 ms | 92 ms | 92 ms | 92 ms | 92 ms |

## Where one median turn spends its time

A single representative turn, so the parts genuinely add up.

| Stage | Time | Share |
|---|---:|---:|
| VAD end-of-turn | 155 ms | 15% |
| ASR | 14 ms | 1% |
| LLM time to first token | 783 ms | 75% |
| TTS time to first byte | 91 ms | 9% |
| **Total** | **1.04 s** | |

## Realtime factor

Median 6.25x, worst 6.02x. Above 1.0x means speech is synthesized faster than it is spoken, which is the condition for a conversation to continue without the audio running dry.

## Errors

| Error | Count |
|---|---:|
| `response_failed` | 2 |
| `response.failed` | 2 |

