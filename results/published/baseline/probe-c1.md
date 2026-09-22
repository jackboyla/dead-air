# Baseline: defaults (Smart Turn on)

1 concurrent session(s), 9 turn(s) each, 1 warmup turn(s) excluded. Started 2026-09-22T08:57:21.961079+00:00.

Parakeet TDT STT, gemma-4-E4B Q4_0 via llama.cpp, Qwen3-TTS GGML. Defaults throughout.

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
| Wall time | 29.1 s |

## Headline latency

| Measure | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| Time to first audio (server clock) | 8 | 895 ms | 905 ms | 905 ms | 905 ms | 905 ms |
| Time to first audio (user clock) | 8 | 1.05 s | 1.07 s | 1.07 s | 1.07 s | 1.07 s |
| Full response | 8 | 416 ms | 477 ms | 477 ms | 477 ms | 477 ms |

## Stage breakdown

Per-stage percentiles are computed independently, so they are not
expected to sum to the headline total — the slowest ASR turn and the
slowest TTS turn are rarely the same turn.

| Stage | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| VAD end-of-turn | 8 | 149 ms | 174 ms | 174 ms | 174 ms | 174 ms |
| ASR | 8 | 15 ms | 26 ms | 26 ms | 26 ms | 26 ms |
| LLM time to first token | 8 | 783 ms | 792 ms | 792 ms | 792 ms | 792 ms |
| TTS time to first byte | 8 | 92 ms | 103 ms | 103 ms | 103 ms | 103 ms |

## Where one median turn spends its time

A single representative turn, so the parts genuinely add up.

| Stage | Time | Share |
|---|---:|---:|
| VAD end-of-turn | 148 ms | 14% |
| ASR | 13 ms | 1% |
| LLM time to first token | 792 ms | 76% |
| TTS time to first byte | 93 ms | 9% |
| **Total** | **1.05 s** | |

## Realtime factor

Median 6.20x, worst 5.92x. Above 1.0x means speech is synthesized faster than it is spoken, which is the condition for a conversation to continue without the audio running dry.

