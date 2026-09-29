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
3. Build `deadair`: an LLM measurement/fault-injection proxy (`tap`), a Realtime
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
- `deadair` ships tap, probe, sweep, report and exporter. 92 tests, no GPU needed.
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

- Join tap records to turn timelines automatically in `deadair report`, rather
  than correlating by wall clock by hand.
- A `--transport webrtc` option for the probe, so load tests exercise the same path
  the browser uses.
- Offer the two upstream findings to `huggingface/speech-to-speech` (see below).

## Findings worth upstreaming

- **A truncated LLM stream is reported as a completed turn.** When the backend hangs
  up mid-stream the pipeline speaks the fragment and reports `status=completed` with
  an empty error list. Nothing on the Realtime event stream distinguishes it from a
  complete answer, so a monitor watching error counts sees a healthy system.
  Reproducible with `deadair probe --fault "truncate@4"`.
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

## Plan 4: fold s2s-bench into dead-air (2026-09-29)

### Why

Two public harnesses for the same protocol split attention. dead-air has the
findings and the stronger probe (two clocks, speech barge-in, speculative
response handling), so it stays. s2s-bench contributes what dead-air lacked for
regression work.

### Ported

- `deadair mock`: deterministic Realtime target with fault drills
  (`--fail-every`, `--max-sessions`, `--stale-audio-after-cancel`, stage delays).
  Turn detection counts silence in audio time, like a real VAD.
- Budgets: `--budget` on `probe`, and `deadair gate` for saved runs. Exit 2 on failure.
- `deadair compare`: stage-by-stage median/p95 deltas; regression = median up >10% and >20 ms.
- Protocol checks in the reducer: `audio_after_response_done`, `audio_before_response_created`.
- CI job that probes the mock with budgets on every push.

### Deliberately not ported

- YAML scenario files: a second config system next to CLI flags. A shell script of
  `deadair probe` flags is the scenario.
- Client-side send jitter and audio drop: conflicts with deadline pacing; network
  faults belong in `tc`/a proxy, as s2s-bench's own README said.
- Perfetto `trace.json` and a Prometheus text file: dead-air already writes JSONL
  traces and runs live Prometheus.
- s2s-bench's interpolated percentiles: dead-air keeps nearest rank.

### Found while porting

- **Refusal race in the probe (fixed).** speech-to-speech sends
  `session_limit_reached` then closes with 1008 at once. If the socket closed before
  the reader handled the error, the probe raised on `session.update` and counted a
  generic failure rather than a refusal. The probe now drains the reader first.
- **Probe frame pacing is one frame early (not changed).** `_stream_pcm` sends each
  20 ms frame at the start of its window; a microphone delivers it at the end. The
  server therefore finishes counting VAD silence ~20 ms sooner than it would with a
  live mic, and perceived TTFA reads ~20 ms low. Published numbers carry this bias.
  Changing it breaks comparison with `results/published/`, so it needs a decision
  and a re-run, not a silent fix.

### Commands

```bash
uv sync --group dev
uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/
uv run mypy src/
uv run pytest tests/ -q            # 108 passed
```

The CI smoke step was also run locally, verbatim from `.github/workflows/ci.yml`: all budgets passed.

### Outstanding

- Archive `jackboyla/s2s-bench` after this lands (Jack to confirm).
- Decide on the frame-pacing bias above.
- Nightly regression loop against speech-to-speech `main` (next plan).
