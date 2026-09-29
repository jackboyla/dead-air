# dead-air

**Where the second goes in a local voice agent.**

[![CI](https://github.com/jackboyla/dead-air/actions/workflows/ci.yml/badge.svg)](https://github.com/jackboyla/dead-air/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](./pyproject.toml)

*Dead air* is what broadcast engineers call the silence when nothing is going out.
In a voice agent it is the gap between a person finishing their sentence and the
machine making a sound — about **one second** on the stack measured here, and
almost none of it is the language model.

This repository is a fully local, OpenAI-Realtime-compatible voice agent running on
two consumer GPUs, plus the tooling that accounts for every millisecond of that
silence. No API keys, no hosted inference, no audio leaving the machine. Speech
recognition, the language model and speech synthesis all run locally behind Hugging
Face [`speech-to-speech`](https://github.com/huggingface/speech-to-speech), which
exposes the whole thing over the OpenAI Realtime WebSocket and WebRTC event set.

Standing that up is the easy part. Explaining the silence is the interesting part,
and the answer turned out to be surprising.

---

## The headline

Measured on one RTX 5090 pair, 8 turns per configuration, medians:

| Stage | Time | Share of the wait |
|---|---:|---:|
| VAD deciding the user stopped talking | 149 ms | 14% |
| ASR (Parakeet TDT) | 15 ms | 1% |
| "LLM" | 783 ms | 75% |
| TTS first byte (Qwen3-TTS) | 92 ms | 9% |
| **First audio, from the user's point of view** | **1.05 s** | |

The language model looks like three quarters of the budget. It is not.

Measured *between the pipeline and llama.cpp*, the same requests take **40 ms to
first token and 88 ms end to end**. The other ~740 ms is
[`speculative_reopen_ms`](docs/latency-budget.md): the pipeline holds the turn open
in case the speaker is only pausing. Set it to 200 ms and perceived latency drops
from **1.05 s to 444 ms** without touching a single model.

That is not a free win, and the second measurement is the point of this repository:

| `speculative_reopen_ms` | Perceived first audio | Talked over a speaker who paused |
|---:|---:|---:|
| 200 | 444 ms | 9 / 12 (75%) |
| 400 | 646 ms | 7 / 12 (58%) |
| **800 (default)** | **1.05 s** | **1 / 12 (8%)** |

**The dominant term in a local voice agent's latency is turn-taking policy, not
inference.** Quoting a time-to-first-audio number without its interruption rate is
quoting half a result. Full working in [`docs/latency-budget.md`](docs/latency-budget.md).

---

## Architecture

```
                    browser (web/)                    deadair probe
                          │                                  │
                    WebRTC │ audio + oai-events        WebSocket │ Realtime events
                          ▼                                  ▼
            ┌───────────────────────────────────────────────────────┐
            │  speech-to-speech  ·  :18765  ·  GPU 1                 │
            │                                                        │
            │   Silero VAD ──▶ Parakeet TDT ──▶ LLM ──▶ Qwen3-TTS    │
            │   + Smart Turn      (STT)          │        (TTS)      │
            └────────────────────────────────────┼───────────────────┘
                     │ /v1/usage, /v1/pool       │ OpenAI Responses API
                     ▼                           ▼
            ┌─────────────────┐         ┌──────────────────────────┐
            │ deadair      │         │ deadair tap  · :18900 │
            │ exporter :18901 │         │ measures + injects faults│
            └────────┬────────┘         └────────────┬─────────────┘
                     │                               │
                     │                               ▼
                     │                  ┌──────────────────────────┐
                     │                  │ llama.cpp · :18080 · GPU0│
                     │                  │ gemma-4-E4B-it GGUF      │
                     │                  └──────────────────────────┘
                     ▼
            Prometheus :19090 ──▶ Grafana :13000
```

Three pieces do the work:

**`deadair tap`** is a streaming reverse proxy between the pipeline and the LLM
server. The Realtime event stream can tell you when the *pipeline* produced its
first token; it cannot tell you how much of that was the model. The tap can, without
forking either project. It also injects faults on demand, because "what happens when
the backend stalls mid-sentence" is only answerable by making it happen.

**`deadair probe`** drives real conversations over the Realtime protocol and
reduces the event stream into a per-turn latency budget. It speaks synthesized
prompts in real time, at microphone pacing, because server-side VAD behaves
differently if you dump an utterance in one frame.

**`deadair report`** turns a run into Markdown and a self-contained HTML page
that opens offline.

Nothing here patches `speech-to-speech`. It is used as a released dependency, so
these measurements stay valid across upgrades.

---

## Quickstart

Needs Linux, an NVIDIA GPU, Docker with the container toolkit, and
[uv](https://docs.astral.sh/uv/). Two GPUs is comfortable; one works with a smaller
pool.

### 1. Install

```bash
git clone https://github.com/jackboyla/dead-air.git
cd dead-air
uv sync --group dev
```

The speech pipeline itself lives in its own environment, because its CUDA and model
dependencies are large and change on a different schedule:

```bash
python3 -m venv ../speech-to-speech/.venv
../speech-to-speech/.venv/bin/pip install "speech-to-speech[webrtc]"
```

### 2. Start the stateless services

llama.cpp, the tap, Prometheus and Grafana. First run downloads ~4.6 GB of weights.

```bash
docker compose up -d
docker compose logs -f llama    # wait for "listening on"
```

Ports default to an `18xxx`/`19xxx` block rather than 8080/8765/3000, which are the
first ports anything else on a shared machine takes. Override them in `.env`.

### 3. Start the pipeline

```bash
./scripts/run-pipeline.sh                  # defaults: Parakeet TDT, Qwen3-TTS, pool of 1
NUM_PIPELINES=4 ./scripts/run-pipeline.sh  # pool of 4, ~23 GB on one card
```

The pipeline binds `127.0.0.1`, because the Realtime API is unauthenticated. The
containerised exporter cannot reach loopback on the host, so pipeline panels in
Grafana stay empty until you opt in with `HOST=0.0.0.0 ./scripts/run-pipeline.sh` on
a network you trust. The [runbook](docs/runbook.md) covers the loopback-only
alternative.

### 4. Talk to it

Open <http://127.0.0.1:18088> and press Connect. The page shows each turn's latency
broken down live, using the same stage definitions the benchmark uses.

To check the WebRTC path without a browser:

```bash
../speech-to-speech/.venv/bin/python scripts/check_webrtc.py
```

### 5. Measure it

```bash
# Prompt audio, generated once and cached. Needs the pipeline venv, which has kokoro.
../speech-to-speech/.venv/bin/python scripts/make_prompts.py --out assets/prompts
../speech-to-speech/.venv/bin/python scripts/make_hesitation_prompts.py --out assets/prompts-hesitation

# Steady-state latency budget
uv run deadair probe --concurrency 1 --turns 9 --warmup 1 --out results/baseline

# How many conversations it sustains, and what gives way first
uv run deadair sweep --levels 1,2,4,6 --turns 7 --out results/sweep

# Interruption handling. Replies must be long enough to interrupt, or you measure nothing.
uv run deadair probe --barge-in-after 1.2 --turns 7 --out results/barge-in \
    --instructions "You are a voice assistant. Answer thoroughly in four or five full sentences."

# Backend failures
uv run deadair probe --turns 6 --fault "slow-start:1500ms" --out results/fault-slow-llm
uv run deadair probe --turns 7 --fault "reject:503;p=0.5"  --out results/fault-reject
uv run deadair probe --turns 6 --fault "truncate@4"        --out results/fault-truncate

# How often a shorter reopen window talks over a speaker who paused
uv run python scripts/false_endpoint.py --label reopen-800ms-default
```

Every command writes a run JSON, a Markdown summary, a standalone HTML report, and
the raw JSONL event trace the numbers came from. Committed results are under
[`results/published/`](results/published); the runs behind them are logged in
[`progress/experiment-log.md`](progress/experiment-log.md).

### 6. Gate and compare

Any probe run can be held to a latency budget. The command exits `2` when a budget
fails, so it works as a CI step or a nightly check:

```bash
uv run deadair probe --turns 9 --warmup 1 --out results/nightly \
    --budget "perceived_ttfa:p95<=1200" --budget "completion_rate>=0.99" --budget "protocol_violations<=0"

# The same check against a run already on disk
uv run deadair gate results/baseline/probe-c1.json --budget "llm_ttft:p50<=100"
```

To find which stage a change slowed, probe both builds the same way and compare:

```bash
uv run deadair compare results/base/probe-c1.json results/candidate/probe-c1.json \
    --out results/compare.md --fail-on-regression
```

A stage counts as a regression when its median rises by more than 10% *and* more
than 20 ms (`--threshold`, `--floor-ms`). Medians, because with a few dozen turns a
p95 is one or two samples.

### Without a GPU

`deadair mock` is a Realtime server with fixed stage delays and no models. It
exists to test the tooling, not to produce numbers anyone should quote. CI runs the
probe against it on every push. To run the same thing locally:

```bash
uv run python scripts/make_tone_prompts.py --out /tmp/tones
uv run deadair mock &
uv run deadair probe --url ws://127.0.0.1:18766/v1/realtime --prompts /tmp/tones --turns 4 \
    --budget "completion_rate>=1" --budget "protocol_violations<=0"
```

It can also misbehave on purpose, so each detector is tested against a known answer:

```bash
uv run deadair mock --fail-every 3                 # failed responses lower completion
uv run deadair mock --max-sessions 1               # sessions past the pool are refused
uv run deadair mock --stale-audio-after-cancel     # audio after response.done is a violation
uv run deadair mock --llm-ms 300                   # compare should blame llm_ttft alone
```

---

## What the measurements found

### Capacity is an admission limit, not a slowdown

| Sessions | Completed | Refused | Perceived first audio p50 | p95 | Realtime factor |
|---:|---:|---:|---:|---:|---:|
| 1 | 100% | 0 | 1048 ms | 1070 ms | 6.24x |
| 2 | 100% | 0 | 1054 ms | 1099 ms | 6.14x |
| 4 | 100% | 1 | 1068 ms | 1105 ms | 5.31x |
| 6 | 100% | 3 | 1062 ms | 1124 ms | 5.42x |

The stack does not degrade under load, it refuses. Sessions past the pool get
`session_limit_reached`, and every admitted session keeps its latency. That is the
behaviour you want, and it means capacity planning is about pool size and VRAM, not
about a latency cliff. A pool of 4 costs ~23 GB on one 32 GB card.

Latency stays flat partly because the dominant term is a fixed timer that does not
care how busy the GPU is.

### Quantisation is invisible here

| | Q4_0 | Q8_0 |
|---|---:|---:|
| Weights | ~4.6 GB | ~8.7 GB |
| Time to first token *at the tap* | 40 ms | 48 ms |
| Total generation *at the tap* | 88 ms | 104 ms |
| Decode rate | 241 tok/s | 185 tok/s |
| **Perceived first audio** | **1.05 s** | **1.05 s** |

Doubling the weight precision costs 8 ms of time to first token and **zero**
perceived latency, because both fit inside the 800 ms reopen window. Quantisation
only becomes a latency lever once generation approaches that window — roughly a 9x
slowdown from here, meaning a much larger model, much longer answers, or enough
concurrency to make llama.cpp queue.

This is the useful kind of negative result: it says don't spend a week on it.

### Barge-in costs 393 ms, and it is VAD, not cancellation

From the user speaking over the assistant to the last audio frame the client still
has to play: **393 ms** (p50, n=6). That is close to the VAD's `min_speech_ms`
default of 384 ms — the pipeline must hear enough speech to believe an interruption
before it cancels. The cancellation itself is quick; the decision to cancel is what
costs.

Measured against the trailing audio frame rather than `response.done` on purpose. A
cancel that is fast on paper but keeps emitting audio is not a fast cancel: that
audio is played over the user's voice.

### Failures, in order of how well they go

| Injected fault | Completed | What the user gets |
|---|---:|---|
| `slow-start:1500ms` | 5/5 | First audio 1.05 s → 1.85 s. No errors. |
| `reject:503;p=0.5` | 4/6 | Failed turns *speak* a fallback line; the session survives. |
| `truncate@4` | 5/5 | A spoken fragment, reported as `completed`, no error anywhere. |

The 503 case degrades the way you would hope. The turn is marked `failed`, the user
hears "I'm having trouble responding right now. Please try again." instead of
silence, and the next turn succeeds on the same session.

The truncation case is the one to worry about. A backend that hangs up mid-stream
produces a spoken fragment — *"Water boils at one"*, *"The tallest mountain in"* —
and the turn is reported `status=completed` with an empty error list. Nothing in the
Realtime event stream distinguishes it from a complete answer. A monitor watching
error counts sees a healthy system.

### The pipeline runs the LLM speculatively, and that is clever

A 1500 ms injected delay only moved first audio by ~800 ms. Correlating the tap's
timestamps against the event trace shows why:

```
LLM request accepted at t=0
      -1 ms   conversation.item.input_audio_transcription.completed
   +1667 ms   response.created
```

The pipeline dispatches the language model 1–4 ms after the transcript lands and
holds the *output* until the reopen window closes. The wait and the inference
overlap, so backend slowness up to the window length costs nothing at all. The
800 ms is not time spent before the model runs; it is time the model runs inside.

---

## Observability

`docker compose up -d` brings up Prometheus and a provisioned Grafana dashboard at
<http://127.0.0.1:13000> covering LLM time to first token, queue wait, worst
inter-token gap, and pipeline pool occupancy by state.

Two deliberate choices. Scrape interval is 5 s, not the 15 s default, because a
voice turn lasts about two seconds and a 15 s interval averages away the thing you
are looking at. Latency histogram buckets are dense between 25 ms and 1 s, where a
turn goes from good to bad, rather than Prometheus's defaults which put most of
their resolution above one second — a region where a voice agent has already lost
the conversation.

The dashboard surfaces `stuck` pipeline units separately. A unit whose session was
released but whose handler never drained is occupied indefinitely and silently
reduces capacity.

---

## Tooling reference

```
deadair tap       measuring, fault-injecting reverse proxy for the LLM backend
deadair exporter  republish the pipeline's /v1/usage and /v1/pool as metrics
deadair probe     drive Realtime sessions and record the latency budget
deadair sweep     run the same scenario at increasing concurrency
deadair report    re-render a saved run without re-running it
deadair gate      check a saved run against latency budgets
deadair compare   compare two saved runs stage by stage
deadair mock      deterministic Realtime target for tests and CI
```

### Protocol violations

Each run also counts events that break the Realtime contract, however good the
latency looks:

| Violation | Meaning |
|---|---|
| `audio_after_response_done` | Audio arrived for a response already reported finished. A client plays it over whatever comes next. |
| `audio_before_response_created` | Audio arrived for a response the server never announced. |

### Fault specifications

Passed to `deadair tap --fault`, to `deadair probe --fault` (applied to a
running tap for the duration of the run), or POSTed to `/faults` to degrade a live
backend without restarting it.

| Spec | Effect |
|---|---|
| `slow-start:1200ms` | Hold the request before contacting upstream |
| `slow-start:200-900ms` | The same, sampled uniformly from a range |
| `stall:400ms@3` | Pause after relaying chunk 3 |
| `truncate@12` | Hang up after relaying chunk 12 |
| `reject:503` | Answer 503 without contacting upstream |
| `…;p=0.1` | Apply any of the above only 10% of the time |

Intermittent is usually the honest setting. A backend that fails every time gets
noticed immediately; the one that fails a tenth of the time is the one that reaches
production.

---

## Measurement notes

Numbers are only worth something if you know how they were taken.

**Two clocks, both reported.** `input_audio_buffer.speech_stopped` is when the
*server's* VAD decided you stopped talking. It necessarily lags when you actually
stopped. Reporting only the server clock hides ~150 ms of real waiting, so both
appear in every report, labelled "server clock" and "user clock".

**Percentiles are nearest-rank, not interpolated.** Every percentile printed is a
latency that actually happened. Interpolation is harmless with a million samples and
misleading with sixty turns.

**Stage medians are not normalised to sum.** The slowest ASR turn and the slowest
TTS turn are usually different turns. Where an additive view is needed, the report
decomposes one real turn — the completed turn nearest the median — so the parts
genuinely add up and can be traced to a line in the event log.

**Warmup is excluded and labelled.** First-turn latency is dominated by lazy model
warmup and an empty KV cache. Mixing it in makes the deployment look far worse than
it is; dropping it silently makes cold start look free.

**Timestamps are taken before parsing.** A large base64 audio delta is not free to
decode, and charging that to the server would inflate exactly the numbers this tool
exists to report.

**Raw traces are written before any statistic is derived**, so every published
number can be recomputed from the event timeline behind it.

## Limitations

- **Prompts are synthesized**, by Kokoro, not recorded from people. TTS audio is
  cleaner than a microphone in a room, so the ASR figures are a lower bound. The
  hesitation prompts use cleanly silent gaps; real hesitations contain breath and
  filler, which Silero may treat differently. The direction of every effect reported
  here is robust; the exact rates belong to these prompts.
- **One hardware configuration.** Everything was measured on 2x RTX 5090 with
  gemma-4-E4B-it. Ratios should travel; absolute numbers will not.
- **Small samples.** Eight measured turns per configuration, twelve trials per
  false-endpoint setting. Enough to separate 444 ms from 1.05 s, or 8% from 75%. Not
  enough for a confident p99.
- **The tap adds a hop.** It is loopback and streams chunk by chunk, and the tests
  assert it does not buffer, but it is not zero. It measures ~1 ms of its own
  overhead against a local upstream.

## Repository layout

```
src/deadair/       tap, probe, report, exporter, mock, CLI
  timeline.py         the Realtime event reducer — where every latency is defined
  report/gate.py      latency budgets
  report/compare.py   run-to-run regression check
web/                  WebRTC browser client with a live latency breakdown
deploy/               Prometheus config, provisioned Grafana dashboard
scripts/              prompt generation, pipeline launcher, WebRTC check
docs/                 latency budget deep dive, runbook
results/published/    committed measurements and the traces behind them
progress/             plan and the append-only experiment ledger
tests/                108 tests, no GPU required
```

## Contributing and prior art

This builds on [`huggingface/speech-to-speech`](https://github.com/huggingface/speech-to-speech)
(Apache-2.0) and uses it unmodified. Two things found along the way are worth
reporting upstream, and are written up in the
[experiment log](progress/experiment-log.md):

- `s2s_pipeline.py` looks for the NLTK POS tagger under `tokenizers/` when NLTK
  installs it under `taggers/`. The lookup always misses, so `nltk.download` runs on
  every start. On a host whose IPv6 routes drop traffic that turned `serve -h` into a
  seven-minute hang; a one-line path fix takes it to six seconds.
- A truncated LLM stream is reported as a completed turn with no error.

The budget gate, `compare`, the mock target and the protocol checks came from
[`s2s-bench`](https://github.com/jackboyla/s2s-bench), an earlier harness for the
same protocol that is now folded into this repository.

See [`docs/runbook.md`](docs/runbook.md) for operating notes, including the IPv6
workaround and what to do when sessions are refused.

## License

Apache-2.0. See [LICENSE](./LICENSE).
