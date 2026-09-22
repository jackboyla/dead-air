# Published results

Committed measurements, each with the run JSON, a Markdown summary, and a
self-contained HTML report. The raw JSONL event traces that produced them are kept
alongside, so every number here can be recomputed rather than taken on trust:

```bash
uv run localvoice report results/published/baseline/probe-c1.json
```

All of it was measured on `radiance-ws`: 2x RTX 5090 (32 GB), 60 GB RAM, driver
580.173.02. gemma-4-E4B-it Q4_0 on llama.cpp, Parakeet TDT 0.6b v3, Qwen3-TTS
12Hz 1.7B. Eight measured turns per configuration with one warmup turn excluded,
unless noted.

| Directory | What it is |
|---|---|
| `baseline/` | Defaults throughout. The reference every other run is compared against. |
| `no-smart-turn/` | `--no_smart_turn`. Tests, and rejects, the first hypothesis about the LLM stage. |
| `spec-reopen-200/`, `spec-reopen-400/` | Shorter speculative turn windows. |
| `false-endpoint/` | How often each window talks over a speaker who paused. |
| `quant-q4/`, `quant-q8/` | Q4_0 against Q8_0, identical procedure, pipeline never restarted. |
| `sweep/` | Concurrency at 1, 2, 4 and 6 sessions. |
| `barge-in/` | Interrupting the assistant 1.2 s into a long reply. |
| `fault-slow-llm/` | LLM 1.5 s slow to start. |
| `fault-reject/` | LLM returns 503 half the time. |
| `fault-truncate/` | LLM hangs up mid-stream. |

Read them alongside [`progress/experiment-log.md`](../../progress/experiment-log.md),
which records what each run was testing, what was decided, and — in two cases —
which hypothesis turned out to be wrong.

The limitations in the [README](../../README.md#limitations) apply to all of it.
Prompts are synthesized rather than recorded, sample sizes are small, and only one
hardware configuration was measured.
