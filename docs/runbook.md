# Runbook

Operating notes for the stack, including the failures met while building it. Every
workaround here exists because something actually went wrong on a real machine.

## Ports

Defaults avoid 8080, 8765 and 3000, which are the first ports anything else on a
shared workstation claims. Override in `.env`.

| Service | Default | Variable |
|---|---:|---|
| llama.cpp | 18080 | `LLAMA_PORT` |
| speech-to-speech Realtime | 18765 | `PORT` (script), `PIPELINE_PORT` (compose) |
| deadair tap | 18900 | `TAP_PORT` |
| deadair exporter | 18901 | `EXPORTER_PORT` |
| Prometheus | 19090 | `PROMETHEUS_PORT` |
| Grafana | 13000 | `GRAFANA_PORT` |
| Web client | 18088 | `WEB_PORT` |

Check before starting, and do not take a port someone else is using:

```bash
ss -tlnp | grep -E ":(18080|18765|18900|18901|19090|13000|18088)"
```

## Health checks

```bash
curl -s http://127.0.0.1:18080/health          # llama.cpp
curl -s http://127.0.0.1:18900/healthz         # tap, reports its upstream
curl -s http://127.0.0.1:18765/v1/pool         # pipeline units and their states
curl -s http://127.0.0.1:18765/v1/usage        # cumulative counters
curl -s http://127.0.0.1:18900/stats           # last 20 LLM requests with timings
```

`/v1/pool` is the one to watch. A unit in `stuck` was released by its client but its
handler never drained; it is occupied indefinitely and silently reduces capacity.

## Starting and stopping

Long-running processes go in tmux, never a foreground shell:

```bash
docker compose up -d
tmux new -d -s da-s2s './scripts/run-pipeline.sh 2>&1 | tee progress/logs/pipeline.log'

# Verify it is actually up, rather than trusting that the launch returned
until curl -sf http://127.0.0.1:18765/v1/pool; do sleep 5; done
nvidia-smi
tail -30 progress/logs/pipeline.log
```

Useful environment variables for `run-pipeline.sh`:

| Variable | Default | Notes |
|---|---|---|
| `GPU` | `1` | Speech models. llama.cpp takes the other card. |
| `NUM_PIPELINES` | `1` | Pool size. 4 costs ~23 GB on one 32 GB card. |
| `LLM_BASE_URL` | `http://127.0.0.1:18900/v1` | The tap, not llama.cpp directly. |
| `STT` / `TTS` | `parakeet-tdt` / `qwen3` | Any backend the upstream CLI accepts. |
| `S2S_VENV` | `../speech-to-speech/.venv` | Where the pipeline is installed. |
| `HOST` | `127.0.0.1` | See below before changing. |

Extra flags pass straight through:

```bash
./scripts/run-pipeline.sh --speculative_reopen_ms 400 --no_smart_turn
```

### Pipeline metrics show nothing in Grafana

**Symptom.** LLM panels have data, pool panels are empty. `s2s_pool_units` returns
no series.

**Cause.** The pipeline binds `127.0.0.1` by default. The exporter runs in a
container and reaches the host through the docker gateway, which loopback does not
answer. This is the safe default doing its job: the Realtime API has no
authentication, so exposing it is opt-in.

Confirm it — the exporter stays up and reports why:

```bash
curl -s http://127.0.0.1:18901/healthz     # last_error names the failure
```

**What to do.** If the machine is on a network you trust, bind wider:

```bash
HOST=0.0.0.0 ./scripts/run-pipeline.sh
```

Do not do this on an untrusted network without putting authentication in front of
the port. If you would rather keep loopback, run the exporter on the host instead
of in compose:

```bash
docker compose stop exporter
uv run deadair exporter --pipeline-url http://127.0.0.1:18765 --host 0.0.0.0 --port 18901
```

## Known failure modes

### Startup hangs for minutes with no output

**Symptom.** `speech-to-speech serve -h` takes over seven minutes. No CPU use, no
log lines.

**Cause.** Two things compounding. The host advertises IPv6 routes that drop
traffic, and CPython's `socket.create_connection` has no Happy Eyeballs: it walks
`getaddrinfo` in order and blocks for the full TCP timeout on every dead AAAA
record. `curl` survives the same network because it races both families.

Confirm it:

```bash
ss -tnp | grep SYN-SENT        # a v6 address stuck in SYN-SENT is the signature
curl -4 -sS -o /dev/null -w "%{http_code}\n" https://raw.githubusercontent.com   # works
curl -6 -sS -o /dev/null -w "%{http_code}\n" https://raw.githubusercontent.com   # hangs
```

The thing being fetched is an NLTK package, on every single start, because
`speech_to_speech/s2s_pipeline.py` looks for the POS tagger at
`tokenizers/averaged_perceptron_tagger_eng` while NLTK installs it under
`taggers/`. The lookup never succeeds, so `nltk.download` always runs.

**Workaround.** Prefetch the assets over IPv4 and expose the tagger where the code
looks for it:

```bash
mkdir -p ~/nltk_data/tokenizers ~/nltk_data/taggers
base=https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages
curl -4 -sSL -o /tmp/punkt_tab.zip "$base/tokenizers/punkt_tab.zip"
curl -4 -sSL -o /tmp/tagger.zip    "$base/taggers/averaged_perceptron_tagger_eng.zip"
unzip -oq /tmp/punkt_tab.zip -d ~/nltk_data/tokenizers/
unzip -oq /tmp/tagger.zip    -d ~/nltk_data/taggers/
ln -sfn ../taggers/averaged_perceptron_tagger_eng ~/nltk_data/tokenizers/averaged_perceptron_tagger_eng
```

Startup goes from a seven-minute hang to about six seconds.

For other downloads on the same host, `scripts/ipv4_only/` contains a
`sitecustomize.py` that narrows Python's resolver to IPv4. Put it on `PYTHONPATH`
and CPython imports it automatically:

```bash
PYTHONPATH=scripts/ipv4_only python whatever.py
```

`run-pipeline.sh` does this already. It is a workaround, not a fix — the fix is to
stop advertising a route that does not work — so keep it out of environments whose
services genuinely need IPv6.

### `session_limit_reached` when you expect free capacity

**Symptom.** A connection is refused even though the pool looked idle, or a loop
that reconnects per utterance fails after the first one.

**Cause.** Releasing a session does not free its pipeline unit instantly. The unit
sits in `draining` until the handler finishes. A fast reconnect races that.

**What to do.** Reuse one connection for a conversation rather than reconnecting per
turn — which is what a real client does anyway. If you must reconnect, wait for the
pool:

```bash
until [ "$(curl -s http://127.0.0.1:18765/v1/pool | jq .in_use)" = "0" ]; do sleep 0.5; done
```

If a unit stays `draining` indefinitely it is reported as `stuck`, which is a
restart, not a wait.

### Barge-in measurements show zero cancellations

**Symptom.** `--barge-in-after` reports a suspiciously fast number with
`Cancelled: 0`.

**Cause.** The replies finished before the interruption landed. You measured a
reply ending naturally, not a cancel.

**What to do.** Make the assistant say more, then interrupt later:

```bash
uv run deadair probe --barge-in-after 1.2 --turns 7 \
    --instructions "You are a voice assistant. Answer thoroughly in four or five full sentences."
```

Check that `Cancelled` is non-zero before believing the number.

### A truncated LLM stream is reported as success

**Symptom.** The assistant says a fragment — *"Water boils at one"* — and every
counter says the turn completed.

**Cause.** When the backend hangs up mid-stream, the pipeline speaks what it
received and reports `status=completed` with an empty error list. Nothing on the
Realtime event stream distinguishes it from a complete answer.

**What to do.** Do not rely on error counts to detect it. The tap does see it: the
request is recorded with `error: "fault:truncate"` for injected cases, and a real
upstream hangup shows up as a short chunk count and an httpx error on the record.
Watching `deadair_llm_requests_total{outcome="error"}` catches what the pipeline's
own metrics miss. Reply length against expectation is the other signal.

Reproduce it deliberately:

```bash
uv run deadair probe --turns 6 --fault "truncate@4" --out results/fault-truncate
```

### Out of memory with a larger pool

Each pipeline unit loads its own speech models. A pool of 4 costs ~23 GB on one
card. Either lower `NUM_PIPELINES`, or put speech models and llama.cpp on separate
GPUs, which is what the defaults do.

```bash
nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv
```

## Degrading a live backend

The tap accepts fault changes at runtime, so you can break a backend mid-conversation
and listen to what happens without restarting anything:

```bash
curl -s -X POST http://127.0.0.1:18900/faults \
     -H 'Content-Type: application/json' \
     -d '{"faults": ["slow-start:2s", "reject:503;p=0.2"]}'

# put it back
curl -s -X POST http://127.0.0.1:18900/faults -d '{"faults": []}'
```

`deadair probe --fault` does this around a run and always restores an empty fault
set afterwards, so a leftover fault cannot poison the next scenario.

## Swapping the language model

The pipeline points at the tap, so changing models means restarting the tap, not the
pipeline. The speech models stay loaded and comparisons stay clean:

```bash
docker run -d --name da-llama-q8 --gpus device=0 -p 18081:8080 \
  -v "$PWD/cache:/root/.cache" -e LLAMA_CACHE=/root/.cache/llama.cpp \
  ghcr.io/ggml-org/llama.cpp:server-cuda \
  -hf ggml-org/gemma-4-E4B-it-GGUF:Q8_0 --alias local-gemma \
  --host 0.0.0.0 --port 8080 -ngl 99 -np 4 -c 16384 -fa on --no-mmproj

tmux kill-session -t da-tap
tmux new -d -s da-tap '.venv/bin/deadair tap --upstream http://127.0.0.1:18081 --port 18900'
```

Keep the `--alias` matching the pipeline's `--model_name`, or requests are rejected
for an unknown model.

## Offline operation

After one online run that caches every model, the stack runs without internet:

```bash
HF_HUB_OFFLINE=1 ./scripts/run-pipeline.sh
```

llama.cpp serves from `LLAMA_CACHE`. Smart Turn needs its ONNX checkpoint cached, or
pass `--smart_turn_model_path`, or `--no_smart_turn`.
