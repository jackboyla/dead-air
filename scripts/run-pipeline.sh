#!/usr/bin/env bash
# Start the speech-to-speech Realtime server against the local LLM tap.
#
# The pipeline runs natively rather than in a container: its CUDA image is large,
# and the native path is what a contributor develops against. Everything stateless
# around it (llama.cpp, the tap, Prometheus, Grafana) lives in compose.yaml.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
S2S_VENV="${S2S_VENV:-${REPO_ROOT}/../speech-to-speech/.venv}"

: "${GPU:=1}"                                   # GPU for the speech models; llama.cpp owns the other
: "${PORT:=18765}"                              # Realtime server port
# Loopback by default: the Realtime API is unauthenticated, so it must be opted
# into network exposure. The compose exporter lives in a container and therefore
# cannot reach 127.0.0.1 on the host, so pipeline metrics in Grafana need
# HOST=0.0.0.0. Only do that on a network you trust. See docs/runbook.md.
: "${HOST:=127.0.0.1}"
: "${LLM_BASE_URL:=http://127.0.0.1:18900/v1}"  # the tap, not llama.cpp directly
: "${MODEL_NAME:=local-gemma}"
: "${STT:=parakeet-tdt}"
: "${TTS:=qwen3}"
: "${NUM_PIPELINES:=1}"
: "${LOG_LEVEL:=info}"

if [[ ! -x "${S2S_VENV}/bin/speech-to-speech" ]]; then
  echo "speech-to-speech not found at ${S2S_VENV}." >&2
  echo "Set S2S_VENV to a virtualenv that has it installed." >&2
  exit 1
fi

# See docs/runbook.md: on a host that advertises IPv6 routes which drop traffic,
# model downloads block for the full TCP timeout instead of falling back.
export PYTHONPATH="${REPO_ROOT}/scripts/ipv4_only${PYTHONPATH:+:${PYTHONPATH}}"
export CUDA_VISIBLE_DEVICES="${GPU}"
export HF_HOME="${HF_HOME:-${HOME}/.cache/huggingface}"

exec "${S2S_VENV}/bin/speech-to-speech" serve \
  --host "${HOST}" \
  --port "${PORT}" \
  --stt "${STT}" \
  --tts "${TTS}" \
  --llm_backend responses-api \
  --model_name "${MODEL_NAME}" \
  --responses_api_base_url "${LLM_BASE_URL}" \
  --responses_api_api_key "" \
  --num_pipelines "${NUM_PIPELINES}" \
  --init_chat_role system \
  --init_chat_prompt "You are a voice assistant. Answer in one short sentence." \
  --log_level "${LOG_LEVEL}" \
  "$@"
