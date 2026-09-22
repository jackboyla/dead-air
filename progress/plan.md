# Session plan

## Ongoing instructions

- Build plan 3: one polished, fully local OpenAI-Realtime-compatible voice deployment
  on top of Hugging Face `speech-to-speech`.
- Target audience is the Hugging Face voice-agents team. Match their repository
  conventions: Apache-2.0, `uv`, ruff (line-length 120), mypy, pytest with
  `asyncio_mode = auto`, CI actions pinned to commit SHAs.
- The deliverable is not "it works". It is: architecture, measured latency
  breakdown, concurrency ceiling, named bottleneck, and observed behaviour under
  barge-in and backend failure.
- Keep every run reproducible and record rerunnable commands in the README.
- Use Jack Boylan's Git identity. No agent co-author trailers.

## Current plan

1. Study upstream `speech-to-speech` (done) and mirror its conventions.
2. Stand up the local stack: llama.cpp CUDA server + Parakeet TDT STT + Qwen3-TTS,
   wired through the OpenAI Realtime API.
3. Build `localvoice`: an LLM measurement/fault-injection proxy (`tap`), a Realtime
   load probe (`probe`), a pool/usage Prometheus exporter, and a report renderer.
4. Ship a WebRTC browser client that surfaces the per-turn latency breakdown live.
5. Measure: latency budget, concurrency sweep, quantisation sweep, barge-in,
   fault injection. Commit the results and the traces behind them.
6. Write the docs: architecture, latency budget, runbook.

## Decisions

- **Measure through a tap, do not patch upstream.** A streaming reverse proxy in
  front of the LLM server yields queue wait, TTFT and inter-token latency without
  forking `speech-to-speech`. Keeps the deployment honest and upgradable.
- **Black-box turn metrics come from the Realtime event stream**, so the same probe
  works against this stack, against hosted OpenAI, or against any compatible server.
- **llama.cpp over vLLM as the primary LLM runtime.** It is what upstream's
  `docker-compose.yml` and README use, it gives a clean GGUF quantisation sweep, and
  it fits a consumer GPU. vLLM is documented as an alternative, not the default.
- **Compose owns the stateless services** (llama.cpp, tap, Prometheus, Grafana).
  The pipeline itself runs natively in a venv, because its CUDA image is large and
  the native path is what a contributor actually develops against.
- Traces are written as JSONL before any statistic is derived, so every published
  number can be recomputed from the raw event timeline.

## Status

Complete. All six plan steps delivered and verified on hardware.

- Stack runs end to end: llama.cpp (GPU 0) + Parakeet TDT + Qwen3-TTS (GPU 1),
  through the OpenAI Realtime API over both WebSocket and WebRTC.
- `localvoice` ships tap, probe, sweep, report and exporter. 92 tests, no GPU needed.
  ruff, ruff format and mypy all clean.
- Compose stack verified live: all three Prometheus targets healthy, every Grafana
  dashboard query returning real data.
- WebRTC verified end to end with `scripts/check_webrtc.py`: prompt transcribed
  correctly, reply synthesized, 54 audio frames received.
- Sidecar image builds and its CLI smoke test passes.
- Measurements recorded in `progress/experiment-log.md`, results under
  `results/published/`.

### Headline finding

The dominant term in perceived latency is turn-taking policy, not inference.
`speculative_reopen_ms` (800 ms default) accounts for ~740 ms of a ~1050 ms budget,
while the language model itself takes 40 ms to first token measured at the tap.
Shrinking the window to 200 ms cuts perceived latency to 444 ms and raises the rate
at which the agent talks over a pausing speaker from 8% to 75%. The default is well
chosen; the trade is the result.

### Bugs found and fixed in this repo

- `asyncio.Barrier.abort()` is a coroutine; called without `await` it silently did
  nothing. Caught by mypy.
- Prometheus scrapes were served as plain text under the OpenMetrics content type,
  so every scrape was rejected with "data does not end with # EOF". Caught by
  running the stack, now covered by a regression test in both metric endpoints.
- The per-session prompt rotation used per-session coprime strides, which collided
  (sessions 1 and 3 both landed on `turn+2`), defeating the cross-session leak
  detection it existed for. Caught by a test; replaced with a plain offset.
- The first barge-in run measured nothing: one-sentence replies ended before the
  interruption landed, so `Cancelled: 0` sat next to a fast-looking number. Re-run
  with longer replies, and documented in the runbook so nobody repeats it.

## Outstanding

Nothing blocking. Possible follow-ups:

- Join tap records to turn timelines automatically in `localvoice report`, rather
  than correlating by wall clock by hand.
- A `--transport webrtc` option for the probe, so load tests exercise the same path
  the browser uses.
- Offer the two upstream findings to `huggingface/speech-to-speech` (see below).

## Findings worth upstreaming

- **A truncated LLM stream is reported as a completed turn.** When the backend hangs
  up mid-stream the pipeline speaks the fragment and reports `status=completed` with
  an empty error list. Nothing on the Realtime event stream distinguishes it from a
  complete answer, so a monitor watching error counts sees a healthy system.
  Reproducible with `localvoice probe --fault "truncate@4"`.
- **NLTK asset lookup bug.** `src/speech_to_speech/s2s_pipeline.py:64` checks
  `nltk.data.find("tokenizers/averaged_perceptron_tagger_eng")`, but NLTK installs
  that package under `taggers/`, not `tokenizers/`. The lookup therefore always
  raises `LookupError` and `nltk.download(...)` runs on every single start.
  On this host, where IPv6 egress is blackholed and Python has no Happy Eyeballs
  fallback, that turned `speech-to-speech serve -h` into a >7 minute hang.
  With the path corrected the same command returns in ~6 s.
  One-line fix: `nltk.data.find("taggers/averaged_perceptron_tagger_eng")`.

## Commands run

- Workstation health checks from the global agent guidelines.
- `git clone --depth 50 https://github.com/huggingface/speech-to-speech.git` into `/tmp/s2s-study`.
- `docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi -L` (GPU runtime OK).
- NLTK asset prefetch over IPv4 (see `docs/runbook.md`).
- `docker pull ghcr.io/ggml-org/llama.cpp:server-cuda`.

## Artifacts

- `progress/plan.md`, `progress/experiment-log.md`, `progress/notes.md`
