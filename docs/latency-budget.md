# What actually determines latency in a local realtime voice agent

Short answer: the turn-taking policy, not the models.

This walks through how that was established, because the conclusion is easy to
state and easy to get wrong. Every number here comes from a run recorded in
[`progress/experiment-log.md`](../progress/experiment-log.md) with its raw event
trace under [`results/published/`](../results/published).

## The stack under measurement

| Component | Choice | Where it runs |
|---|---|---|
| VAD | Silero v5 + Smart Turn v3.2 | GPU 1 / CPU |
| STT | Parakeet TDT 0.6b v3 | GPU 1 |
| LLM | gemma-4-E4B-it, Q4_0 GGUF, llama.cpp | GPU 0 |
| TTS | Qwen3-TTS 12Hz 1.7B, GGML | GPU 1 |
| Serving | `speech-to-speech` 1.0.0, OpenAI Realtime over WebSocket and WebRTC | — |

Hardware: 2x RTX 5090 (32 GB each), 60 GB RAM.

## Defining the stages

A turn's latency is only meaningful against a definition, so here is the one used
everywhere in this repository. All of it is derived from events on the wire, which
means the same reducer works against this stack or against hosted OpenAI.

| Stage | From | To |
|---|---|---|
| VAD end-of-turn | the probe stops sending speech | `input_audio_buffer.speech_stopped` |
| ASR | `input_audio_buffer.speech_stopped` | `…input_audio_transcription.completed` |
| LLM | `…input_audio_transcription.completed` | first output delta |
| TTS | first output delta | first `response.output_audio.delta` |

Two clocks matter and they are not the same clock. The server's VAD decides you
stopped talking some time *after* you actually did. Reporting from
`speech_stopped` alone drops that interval on the floor even though the user spent
it waiting. Both are kept: "server clock" starts at `speech_stopped`, "user clock"
starts when the probe stopped sending speech.

## The baseline

One session, 8 measured turns, defaults throughout. Medians.

| Stage | Time | Share |
|---|---:|---:|
| VAD end-of-turn | 149 ms | 14% |
| ASR | 15 ms | 1% |
| LLM | 783 ms | 75% |
| TTS | 92 ms | 9% |
| **Perceived first audio** | **1.05 s** | |

Parakeet TDT transcribing a two-second utterance in 15 ms is the least interesting
number here, in the best way: speech recognition is solved for this workload.

The LLM stage at 783 ms is where attention goes. And it is a red herring.

## The LLM is not the LLM stage

`deadair tap` sits between the pipeline and llama.cpp and times every request.
Those same turns, measured at the tap:

| | Median |
|---|---:|
| Time to first token | 40 ms |
| Total generation | 88 ms |
| Decode rate | 241 tok/s |

40 ms at the tap, 783 ms at the client. Roughly 740 ms of the "LLM stage" is not
the language model.

The raw trace shows the gap sitting *before* the model is called at all:

```
    0.0 ms  input_audio_buffer.speech_stopped
   13.0 ms  conversation.item.input_audio_transcription.completed
  804.9 ms  response.created                    <- ~790 ms of apparently nothing
  805.0 ms  response.output_audio_transcript.delta
  897.6 ms  response.output_audio.delta
```

### A wrong hypothesis, for the record

The first guess was Smart Turn, whose `smart_turn_incomplete_delay_ms` defaults to
600 ms and is documented as delaying STT and LLM processing. Plausible, and wrong:
running with `--no_smart_turn` changed the LLM stage by 1 ms, from 783 ms to 784 ms.

The cause is `speculative_reopen_ms`, default **800 ms**:

> Keep a soft-ended Realtime turn reopenable for this many milliseconds unless a
> response commits it.

The pipeline holds the turn open in case the speaker is only drawing breath.
Cutting it to 200 ms removed 601 ms of the 783 ms.

| `speculative_reopen_ms` | LLM stage | Perceived first audio |
|---:|---:|---:|
| 800 (default) | 783 ms | 1.05 s |
| 400 | 380 ms | 646 ms |
| 200 | 182 ms | 444 ms |

### The nuance that makes the design good

It would be easy to call that 800 ms wasted. It is not, because the pipeline runs
the language model *inside* the window rather than after it.

Injecting a 1500 ms delay at the tap moved perceived latency by only ~800 ms. If
the model ran after the window, the two would have added. Correlating the tap's
request timestamps against the event trace:

```
LLM request accepted at t=0
      -1 ms   conversation.item.input_audio_transcription.completed
   +1667 ms   response.created
```

The request is dispatched 1–4 ms after the transcript lands. The wait and the
inference overlap. Backend slowness costs nothing until it exceeds the window.

So the correct statement is not "800 ms is spent before the LLM". It is: **the
turn-taking window sets a floor on response time, and the language model is free
as long as it finishes underneath it.** Which, at 88 ms, it comfortably does.

## What the window buys

`speculative_reopen_ms` exists to stop the agent answering someone who has only
paused. Turning it down without measuring that is how you ship an agent that
interrupts people.

`scripts/false_endpoint.py` speaks six questions, each split by one silent gap of
300–900 ms, twice each. A trial counts as interrupted when the server opened more
than one response, or when no transcript contained the end of the question.

| `speculative_reopen_ms` | Perceived first audio | Interrupted the speaker |
|---:|---:|---:|
| 200 | 444 ms | 9 / 12 (75%) |
| 400 | 646 ms | 7 / 12 (58%) |
| 800 (default) | 1.05 s | 1 / 12 (8%) |

At 200 ms the failure is exactly what it sounds like:

```
CUT capital_300   responses=2  transcripts=['What is the capital?', 'Of France,']
CUT boil_500      responses=2  transcripts=['At what temperature does water', 'Boil at sea level.']
```

The agent answers "What is the capital?" while the speaker is still saying "of
France".

The single failure at the default is the prompt with a 900 ms gap, longer than the
800 ms window. The setting behaves exactly as specified.

**A time-to-first-audio figure quoted without its interruption rate is half a
result.** The default is well chosen. If you do lower it, lower it knowing the
trade, and measure the trade on your own audio — these gaps are cleanly silent,
where real hesitations contain breath and filler.

## Where the remaining time goes

With the window understood, the rest of the budget is small and unglamorous.

**VAD end-of-turn, ~149 ms.** Silero's `min_silence_ms` (64 ms default) plus
processing. Also policy rather than compute, and also a trade: shorter means
quicker, and quicker to cut people off.

**ASR, 15 ms.** Not worth optimising.

**TTS first byte, 92 ms.** The realtime factor is 6.2x, meaning speech is
synthesized six times faster than it is spoken. There is enormous headroom here,
which is why concurrency does not move latency.

**Barge-in, 393 ms.** From speaking over the assistant to the last audio frame the
client still has to play. Close to the VAD's `min_speech_ms` default of 384 ms:
the pipeline must hear enough speech to believe an interruption. The cancellation
is quick, the decision is not.

Every one of these is a turn-taking parameter. On this hardware the models are not
the constraint anywhere in the pipeline.

## If you want a faster agent

In order of leverage:

1. **Tune `speculative_reopen_ms` against your own false-endpoint rate.** The only
   large lever, and the only one with a real cost. Measure both sides.
2. **Tune the VAD window** (`min_silence_ms`, `speech_pad_ms`). Same trade, smaller
   magnitude.
3. **Tune `min_speech_ms` if barge-in feels sluggish.** Same trade again, this time
   against spurious interruptions from background noise.
4. **Do not bother with quantisation.** Q4_0 to Q8_0 costs 8 ms of time to first
   token and zero perceived latency. See the [README](../README.md#quantisation-is-invisible-here).
5. **Do not bother with a faster GPU for the LLM**, until generation approaches the
   reopen window. From 88 ms, that is a 9x margin.

This ordering is specific to a small local model answering in a sentence. Larger
models, longer answers, or enough concurrency to make llama.cpp queue all move the
language model back up the list — at which point the tap tells you, because queue
wait and time to first token are separate metrics on the dashboard.

## Reproducing

```bash
docker compose up -d
./scripts/run-pipeline.sh

uv run deadair probe --concurrency 1 --turns 9 --warmup 1 --out results/baseline

# Restart the pipeline with --speculative_reopen_ms 200 (or 400), then:
uv run deadair probe --concurrency 1 --turns 9 --warmup 1 --out results/spec-reopen-200
uv run python scripts/false_endpoint.py --label reopen-200ms
```
