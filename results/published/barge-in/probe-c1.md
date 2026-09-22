# Barge-in: interrupt 1.2 s into a long reply

1 concurrent session(s), 7 turn(s) each, 1 warmup turn(s) excluded. Started 2026-09-22T09:10:09.357771+00:00.

Instructions ask for a four-to-five sentence answer so there is speech left to interrupt; the probe starts speaking 1.2 s after the assistant's first audio frame.

Host `radiance-ws`, NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02; NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02

## Outcome

| Measure | Value |
|---|---|
| Turns measured | 7 |
| Completed | 2 (28.6%) |
| Cancelled | 4 |
| Failed | 0 |
| Timed out | 0 |
| Sessions refused | 0 / 1 |
| Wall time | 45.6 s |

## Headline latency

| Measure | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| Time to first audio (server clock) | 6 | 893 ms | 901 ms | 901 ms | 901 ms | 901 ms |
| Time to first audio (user clock) | 6 | 1.04 s | 1.05 s | 1.05 s | 1.05 s | 1.05 s |
| Full response | 6 | 1.70 s | 1.86 s | 1.86 s | 1.86 s | 1.86 s |
| Barge-in to last audio | 6 | 393 ms | 404 ms | 404 ms | 404 ms | 404 ms |

## Stage breakdown

Per-stage percentiles are computed independently, so they are not
expected to sum to the headline total — the slowest ASR turn and the
slowest TTS turn are rarely the same turn.

| Stage | n | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| VAD end-of-turn | 7 | 152 ms | 155 ms | 155 ms | 155 ms | 155 ms |
| ASR | 7 | 13 ms | 15 ms | 15 ms | 15 ms | 15 ms |
| LLM time to first token | 6 | 787 ms | 798 ms | 798 ms | 798 ms | 798 ms |
| TTS time to first byte | 6 | 92 ms | 102 ms | 102 ms | 102 ms | 102 ms |

## Where one median turn spends its time

A single representative turn, so the parts genuinely add up.

| Stage | Time | Share |
|---|---:|---:|
| VAD end-of-turn | 151 ms | 14% |
| ASR | 10 ms | 1% |
| LLM time to first token | 793 ms | 76% |
| TTS time to first byte | 92 ms | 9% |
| **Total** | **1.05 s** | |

## Realtime factor

Median 6.47x, worst 5.98x. Above 1.0x means speech is synthesized faster than it is spoken, which is the condition for a conversation to continue without the audio running dry.

