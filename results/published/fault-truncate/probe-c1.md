# Fault: LLM hangs up mid-sentence

1 concurrent session(s), 6 turn(s) each, 1 warmup turn(s) excluded. Started 2026-09-22T09:12:17.964877+00:00.

**Injected faults:** `truncate@4`

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
| Wall time | 18.1 s |

## Headline latency

| Measure | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| Time to first audio (server clock) | 5 | 837 ms | 838 ms | 838 ms | 838 ms | 838 ms |
| Time to first audio (user clock) | 5 | 991 ms | 1.01 s | 1.01 s | 1.01 s | 1.01 s |
| Full response | 5 | 187 ms | 282 ms | 282 ms | 282 ms | 282 ms |

## Stage breakdown

Per-stage percentiles are computed independently, so they are not
expected to sum to the headline total — the slowest ASR turn and the
slowest TTS turn are rarely the same turn.

| Stage | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| VAD end-of-turn | 5 | 157 ms | 169 ms | 169 ms | 169 ms | 169 ms |
| ASR | 5 | 12 ms | 25 ms | 25 ms | 25 ms | 25 ms |
| LLM time to first token | 5 | 786 ms | 797 ms | 797 ms | 797 ms | 797 ms |
| TTS time to first byte | 5 | 31 ms | 41 ms | 41 ms | 41 ms | 41 ms |

## Where one median turn spends its time

A single representative turn, so the parts genuinely add up.

| Stage | Time | Share |
|---|---:|---:|
| VAD end-of-turn | 155 ms | 16% |
| ASR | 12 ms | 1% |
| LLM time to first token | 783 ms | 79% |
| TTS time to first byte | 41 ms | 4% |
| **Total** | **991 ms** | |

## Realtime factor

Median 5.85x, worst 5.72x. Above 1.0x means speech is synthesized faster than it is spoken, which is the condition for a conversation to continue without the audio running dry.

