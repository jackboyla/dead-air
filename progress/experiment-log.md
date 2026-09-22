# Experiment log

Append-only research ledger for this repository. Newest entries at the bottom.

## 2026-09-22—ENV-001

### Intent

Establish whether the Hugging Face `speech-to-speech` pipeline runs on this
workstation at all, before designing any measurement around it. Baseline: none.

### Environment

- Machine: `radiance-ws`
- GPUs used: none yet (import-time check only)
- CUDA_VISIBLE_DEVICES: unset
- Git commit: n/a (pre-repo)
- Branch: n/a
- Python environment: `../speech-to-speech/.venv` (Python 3.11.15, torch 2.11.0+cu130)
- Docker image, if applicable: `nvidia/cuda:12.6.0-base-ubuntu24.04` for the runtime check

### Resource check before launch

```bash
nvidia-smi
df -h
free -h
docker ps
tmux ls || true
```

Result: 2x RTX 5090 idle at 36 C / 8 W, 60 GB RAM, 229 GB disk free (87% used),
no containers, no tmux sessions.

### Commands

#### Diagnostics

```bash
docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi -L
.venv/bin/python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
time .venv/bin/speech-to-speech serve -h
```

### Artifacts

- Logs: `/tmp/s2s_help.log`

### Result

- Docker GPU runtime works; both 5090s visible inside a container.
- torch 2.11.0+cu130 reports CUDA available and names the RTX 5090.
- `speech-to-speech serve -h` initially hung for over 7 minutes. The process sat in
  `SYN-SENT` to `[2606:50c0:8000::154]:443`. IPv6 egress is blackholed on this host;
  `curl -6 https://raw.githubusercontent.com` fails while `curl -4` returns in 80 ms.
  curl survives it through Happy Eyeballs, Python's blocking connect does not.
- Root cause is an upstream path bug, not just the network. `s2s_pipeline.py:64` looks
  for the POS tagger at `tokenizers/averaged_perceptron_tagger_eng`; NLTK installs it
  under `taggers/`. The lookup always misses, so `nltk.download(...)` runs on every
  start and blocks on the dead IPv6 route.
- After prefetching both NLTK packages over IPv4 and exposing the tagger at the path
  the code looks for, the same command completes in **5.7 s user time**.

### Decision

Keep. The stack is viable on this hardware. Record the NLTK path bug as an upstream
contribution candidate and ship an IPv4 asset prefetch script in this repo's runbook,
since any host without working IPv6 hits the same wall.

### Notes

- Repeated-output diagnostics? n/a
- GPU utilization or memory issue? None; nothing loaded yet.
- Data issue? Missing NLTK assets, now prefetched.
- Code issue? Yes, upstream `nltk.data.find` path. Candidate one-line PR.
- Next: pull `ghcr.io/ggml-org/llama.cpp:server-cuda` and bring up the LLM server.

## 2026-09-22—LAT-001

### Intent

Establish the steady-state latency budget of the fully local stack, and find which
stage dominates it. Baseline for every later run.

### Environment

- Machine: `radiance-ws`
- GPUs used: GPU 0 llama.cpp, GPU 1 Parakeet TDT + Qwen3-TTS
- CUDA_VISIBLE_DEVICES: `1` for the pipeline; llama.cpp pinned to device 0 by Docker
- Git commit: pre-first-commit
- Branch: main
- Python environment: `.venv` (localvoice), `../speech-to-speech/.venv` (pipeline)
- Docker image: `ghcr.io/ggml-org/llama.cpp:server-cuda`

### Resource check before launch

```bash
nvidia-smi   # GPU0 3950 MiB (llama.cpp), GPU1 7220 MiB (speech models)
df -h        # 217 GB free
tmux ls      # lrv-llama via docker, lrv-tap, lrv-s2s
```

### Commands

#### Setup

```bash
docker run -d --name lrv-llama --gpus device=0 -p 18080:8080 \
  -v /home/jack/workspace/models/llamacpp-cache:/root/.cache \
  -e LLAMA_CACHE=/root/.cache/llama.cpp \
  ghcr.io/ggml-org/llama.cpp:server-cuda \
  -hf ggml-org/gemma-4-E4B-it-GGUF:Q4_0 --alias local-gemma \
  --host 0.0.0.0 --port 8080 -ngl 99 -np 4 -c 16384 -fa on --no-mmproj

tmux new -d -s lrv-tap './scripts/... localvoice tap --upstream http://127.0.0.1:18080 --port 18900 --trace progress/logs/tap.jsonl'
tmux new -d -s lrv-s2s './scripts/run-pipeline.sh'
```

#### Eval

```bash
uv run localvoice probe --concurrency 1 --turns 9 --warmup 1 --out results/baseline
uv run localvoice probe --concurrency 1 --turns 9 --warmup 1 --out results/no-smart-turn      # --no_smart_turn
uv run localvoice probe --concurrency 1 --turns 9 --warmup 1 --out results/spec-reopen-200    # --speculative_reopen_ms 200
```

### Artifacts

- Metrics: `results/baseline/`, `results/no-smart-turn/`, `results/spec-reopen-200/`
- Event traces: `results/*/traces-probe-c1/session_000.jsonl`
- LLM traces: `progress/logs/tap.jsonl`

### Result

Steady-state medians over 8 measured turns, one session.

| Stage | Baseline | `--no_smart_turn` | `--speculative_reopen_ms 200` |
|---|---:|---:|---:|
| VAD end-of-turn | 149 ms | 124 ms | 149 ms |
| ASR (Parakeet TDT) | 15 ms | 15 ms | 16 ms |
| LLM time to first token | 783 ms | 784 ms | **182 ms** |
| TTS time to first byte | 92 ms | 92 ms | 94 ms |
| **Perceived TTFA (p50)** | **1.05 s** | 1.02 s | **444 ms** |

The headline finding: what the client sees as "LLM latency" is almost entirely not
the language model. The tap, sitting between the pipeline and llama.cpp, measured
the same requests at **41 ms time to first token, 78-95 ms total**. The remaining
~740 ms is the pipeline holding the turn open.

Raw trace of one turn shows the gap precisely — it sits *before* the LLM is called:

```
    0.0 ms  input_audio_buffer.speech_stopped
   13.0 ms  conversation.item.input_audio_transcription.completed
  804.9 ms  response.created          <- ~790 ms of nothing
  805.0 ms  response.output_audio_transcript.delta
  897.6 ms  response.output_audio.delta
```

First hypothesis was Smart Turn's `smart_turn_incomplete_delay_ms` (600 ms default).
**Wrong** — disabling Smart Turn changed TTFT by 1 ms. The cause is
`speculative_reopen_ms`, default 800 ms: the turn is kept reopenable in case the
speaker resumes, and no response commits until that window closes. Cutting it to
200 ms removed 601 ms of the 783 ms.

### Decision

Keep the baseline as the default-configuration reference. Record
`speculative_reopen_ms` as the single highest-leverage latency knob in the stack.
Do not recommend lowering it without measuring what it costs, which is the next run:
the window exists to stop the agent interrupting a speaker who pauses mid-sentence.

### Notes

- Repeated-output diagnostics? No repetition observed; transcripts matched prompts.
- GPU utilization or memory issue? No. Both GPUs well under capacity, near 0% between turns.
- Data issue? Prompts are TTS-generated (Kokoro), so ASR latency is a lower bound.
- Code issue? None in this repo. One upstream doc gap: the latency cost of
  `speculative_reopen_ms` is not stated where someone tuning for latency would look.
- Next: quantify the false-endpoint cost of shrinking the reopen window.

## 2026-09-22—TURN-001

### Intent

Quantify what the latency win from LAT-001 costs. `speculative_reopen_ms` exists to
stop the agent answering a speaker who has only paused. LAT-001 showed cutting it
from 800 ms to 200 ms removes 601 ms of perceived latency; this run measures how
often that shorter window makes the agent talk over the user.

### Environment

- Machine: `radiance-ws`
- GPUs used: GPU 0 llama.cpp, GPU 1 speech models
- Branch: main
- Python environment: `.venv` and `../speech-to-speech/.venv`

### Commands

#### Setup

```bash
../speech-to-speech/.venv/bin/python scripts/make_hesitation_prompts.py --out assets/prompts-hesitation
```

#### Eval

```bash
# Restart the pipeline at each window, then:
uv run localvoice probe --concurrency 1 --turns 9 --warmup 1 --out results/spec-reopen-<N>
uv run python scripts/false_endpoint.py --label reopen-<N>ms --repeats 2
```

### Artifacts

- Metrics: `results/spec-reopen-200/`, `results/spec-reopen-400/`, `results/baseline/`
- Eval summary: `results/false-endpoint/*.json`

### Result

Six prompts, each one question split by a single silent gap of 300-900 ms, spoken
twice: 12 trials per setting. A trial counts as interrupted when the server opened
more than one response, or when no transcript contained the end of the question.

| `speculative_reopen_ms` | Perceived TTFA p50 | Interrupted the speaker |
|---:|---:|---:|
| 200 | 444 ms | 9 / 12 (75%) |
| 400 | 646 ms | 7 / 12 (58%) |
| 800 (default) | 1.05 s | 1 / 12 (8%) |

The single failure at the default is `mountain_900`, whose 900 ms gap is longer than
the 800 ms window. The setting behaves exactly as specified.

Verbatim evidence of the failure mode at 200 ms:

```
CUT capital_300   responses=2  transcripts=['What is the capital?', 'Of France,']
CUT boil_500      responses=2  transcripts=['At what temperature does water', 'Boil at sea level.']
```

The agent answers "What is the capital?" while the speaker is still saying "of France".

### Decision

Keep the 800 ms default. Reject the tempting conclusion from LAT-001 that the reopen
window is free latency. Publish both numbers together in `docs/latency-budget.md`:
a TTFA figure quoted without its interruption rate is not a meaningful figure.

### Notes

- Repeated-output diagnostics? None.
- GPU utilization or memory issue? No.
- Data issue? Synthetic gaps are cleanly silent; real hesitations contain breath and
  filler, which Silero may treat differently. The direction of the effect is robust,
  the exact rates are specific to these prompts.
- Code issue? Reconnecting per prompt raced the server's session drain and returned
  `session_limit_reached`. Fixed by speaking every prompt down one connection, which
  is closer to a real conversation anyway. Worth a runbook note.
- Next: concurrency sweep with `--num_pipelines 4`.

## 2026-09-22—LOAD-001

### Intent

Find the concurrency ceiling, the barge-in cost, and the behaviour under four
injected backend faults. Compared against LAT-001's single-session baseline.

### Environment

- Machine: `radiance-ws`
- GPUs used: GPU 0 llama.cpp (`-np 4`), GPU 1 speech models (`--num_pipelines 4`)
- Branch: main

### Resource check before launch

```bash
nvidia-smi   # GPU0 4014 MiB, GPU1 22868 MiB with a 4-unit pool
```

A 4-unit pool costs 22.9 GB on one RTX 5090. Four is close to the practical
ceiling for this card with Parakeet TDT and Qwen3-TTS loaded per unit.

### Commands

#### Eval

```bash
uv run localvoice sweep --levels 1,2,4,6 --turns 7 --warmup 1 --out results/sweep
uv run localvoice probe --barge-in-after 1.2 --turns 7 --out results/barge-in \
    --instructions "Answer thoroughly in four or five full sentences."
uv run localvoice probe --turns 6 --fault "slow-start:1500ms" --out results/fault-slow-llm
uv run localvoice probe --turns 7 --fault "reject:503;p=0.5"  --out results/fault-reject
uv run localvoice probe --turns 6 --fault "truncate@4"        --out results/fault-truncate
```

### Artifacts

- Metrics: `results/sweep/`, `results/barge-in/`, `results/fault-*/`
- LLM traces: `progress/logs/tap.jsonl`

### Result

**Concurrency.** Latency is flat; capacity is an admission limit.

| Sessions | Completed | Refused | Perceived TTFA p50 | p95 | Realtime factor |
|---:|---:|---:|---:|---:|---:|
| 1 | 100% | 0 | 1048 ms | 1070 ms | 6.24x |
| 2 | 100% | 0 | 1054 ms | 1099 ms | 6.14x |
| 4 | 100% | 1 | 1068 ms | 1105 ms | 5.31x |
| 6 | 100% | 3 | 1062 ms | 1124 ms | 5.42x |

The stack does not slow down under load, it refuses. Sessions beyond the pool get
`session_limit_reached` and every admitted session keeps its latency. TTFA is flat
because the dominant term is a fixed timer, not contention, and both GPUs have
headroom. One session was refused at concurrency 4 against a 4-unit pool: a released
session's unit does not free instantly, so a reconnect can race the drain.

**Barge-in.** 393 ms p50 from the user speaking over the assistant to the last audio
frame the client still has to play (n=6, 4 turns cancelled cleanly).

An earlier attempt reported 38 ms with zero cancellations, which was not a
measurement: the one-sentence replies finished before the interruption landed. The
run was redone with instructions asking for four or five sentences. 393 ms is close
to the VAD's `min_speech_ms` default of 384 ms — the pipeline must hear enough
speech to believe an interruption before it will cancel.

**Faults.**

| Injected fault | Completed | Behaviour |
|---|---:|---|
| `slow-start:1500ms` | 5/5 | TTFA 1.05 s -> 1.85 s. No errors. |
| `reject:503;p=0.5` | 4/6 | Failed turns *speak* a fallback line, session survives. |
| `truncate@4` | 5/5 | Fragment spoken, reported as `completed`, no error. |

The 503 case degrades well. The turn is marked `failed`, and the user hears
"I'm having trouble responding right now. Please try again." rather than silence.
The session stays open and the next turn succeeds.

The truncation case is the one to worry about. A backend that hangs up mid-stream
produces a spoken fragment — "Water boils at one", "The tallest mountain in" — and
the turn is reported `status=completed` with an empty `errors` list. Nothing in the
Realtime event stream distinguishes it from a complete answer.

**Speculative LLM dispatch, verified.** A 1500 ms injected delay only moved TTFA by
~800 ms. Correlating the tap's request timestamps against the event trace shows why:

```
LLM request accepted at t=0
      -1 ms  conversation.item.input_audio_transcription.completed
   +1667 ms  response.created
```

The pipeline dispatches the LLM 1-4 ms after the transcript completes and holds the
*output* until the reopen window closes. The 800 ms wait and the LLM call overlap.
This refines LAT-001: the window is not 800 ms spent before the model runs, it is
800 ms the model runs inside. Backend slowness up to the window length costs nothing.

### Decision

Keep. Report the truncation blind spot as the main observability gap found, and
the speculative dispatch as the main design strength. Both are candidates for
upstream discussion.

### Notes

- Repeated-output diagnostics? None.
- GPU utilization or memory issue? Pool of 4 at 22.9 GB is near the card's practical limit.
- Data issue? None.
- Code issue? The first barge-in run measured nothing. Fixed by lengthening replies;
  worth a warning in the docs so nobody repeats it.
- Next: quantisation sweep, Q4_0 against Q8_0.

## 2026-09-22—QUANT-001

### Intent

Test whether LLM quantisation is worth tuning for latency on this stack. Compared
against LAT-001, which showed the language model contributes ~40 ms of a ~1050 ms
budget. Prediction: quantisation is invisible to the user until the model's total
time exceeds the speculative reopen window.

### Environment

- Machine: `radiance-ws`
- GPUs used: GPU 0 runs both llama.cpp servers (Q4_0 on :18080, Q8_0 on :18081), GPU 1 speech
- Branch: main
- Docker image: `ghcr.io/ggml-org/llama.cpp:server-cuda`

### Resource check before launch

```bash
nvidia-smi   # GPU0 9961 MiB with both quantisations resident, GPU1 24968 MiB
df -h        # llama.cpp cache 12 GB
```

### Commands

#### Setup

```bash
docker run -d --name lrv-llama-q8 --gpus device=0 -p 18081:8080 \
  -v /home/jack/workspace/models/llamacpp-cache:/root/.cache \
  -e LLAMA_CACHE=/root/.cache/llama.cpp \
  ghcr.io/ggml-org/llama.cpp:server-cuda \
  -hf ggml-org/gemma-4-E4B-it-GGUF:Q8_0 --alias local-gemma \
  --host 0.0.0.0 --port 8080 -ngl 99 -np 4 -c 16384 -fa on --no-mmproj
```

#### Eval

```bash
# Point the tap at each server in turn; the pipeline never moves.
localvoice tap --upstream http://127.0.0.1:18080 --port 18900 --trace progress/logs/tap-q4.jsonl
uv run localvoice probe --concurrency 1 --turns 9 --warmup 1 --out results/quant-q4

localvoice tap --upstream http://127.0.0.1:18081 --port 18900 --trace progress/logs/tap-q8.jsonl
uv run localvoice probe --concurrency 1 --turns 9 --warmup 1 --out results/quant-q8
```

### Artifacts

- Metrics: `results/quant-q4/`, `results/quant-q8/`
- LLM traces: `progress/logs/tap-q4.jsonl`, `progress/logs/tap-q8.jsonl`

### Result

gemma-4-E4B-it, 8 measured turns each, medians.

| | Q4_0 | Q8_0 |
|---|---:|---:|
| Weights on disk | ~4.6 GB | ~8.7 GB |
| LLM TTFT *at the tap* | 40 ms | 48 ms |
| LLM total *at the tap* | 88 ms | 104 ms |
| Decode rate | 241 tok/s | 185 tok/s |
| Client-observed LLM stage | 785 ms | 786 ms |
| **Perceived TTFA** | **1.05 s** | **1.05 s** |

Doubling the weight precision costs 8 ms of time to first token and 16 ms of total
generation, and **zero** perceived latency. Both fit entirely inside the 800 ms
speculative reopen window, so the user cannot tell them apart.

### Decision

Keep Q4_0 as the default for its smaller footprint, but record that the choice is
free either way at this model size. Quantisation only becomes a latency lever once
the model's total generation time approaches the reopen window — roughly a 9x
slowdown from here, which means a much larger model, a much longer answer, or
enough concurrent sessions to make llama.cpp queue.

### Notes

- Repeated-output diagnostics? None.
- GPU utilization or memory issue? Both servers resident on one card at 10 GB total.
- Data issue? None; identical prompts and procedure across both arms.
- Code issue? None.
- Useful property of the design: swapping the LLM meant restarting the tap, not the
  pipeline. The speech models stayed loaded, so the arms are otherwise identical.
