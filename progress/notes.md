# Notes

Working notes kept alongside `plan.md` and the append-only `experiment-log.md`.

## Upstream conventions mirrored

Reviewed `huggingface/speech-to-speech` at v1.0.0 before writing anything, and
copied its choices rather than inventing:

- Apache-2.0, `uv` with a `dev` dependency group, setuptools with a `src/` layout.
- ruff with `line-length = 120` and `select = ["E", "F", "I", "W"]`, `ignore = ["E501"]`.
- mypy with `ignore_missing_imports`, `warn_unused_configs`.
- pytest with `asyncio_mode = "auto"`.
- CI jobs split into ruff / ruff format / mypy / pytest / package, with every
  GitHub Action pinned to a commit SHA and `permissions: {}` at the top level.
- README style: tables over prose, explicit caveats, memory figures labelled as
  planning estimates rather than measured minimums.

## Protocol details worth remembering

- The Realtime server's native audio rate is 16 kHz, selected by *omitting* the
  format from `session.update`. Only 24 kHz is spelled out, as
  `{"type": "audio/pcm", "rate": 24000}`. Anything else is rejected.
- WebRTC handshake: `POST /v1/realtime/calls` with `Content-Type: application/sdp`.
  It answers **201**, not 200, with the call id in the `Location` header. Events
  travel on a data channel that must be labelled exactly `oai-events`; any other
  label is ignored with a warning.
- `/v1/usage` and `/v1/pool` are useful and undocumented as metrics sources. The
  pool endpoint distinguishes `idle`, `active`, `draining` and `stuck`, and `stuck`
  is the one that matters operationally.
- `GlobalUsageMetrics` carries a comment reading
  `# latency tts, llm, vad, stt (mean, max, p90)` — per-stage latency is a known
  gap upstream, which is roughly what this repository measures from outside.

## Things that cost time

- Python has no Happy Eyeballs. On a host with dead IPv6 routes, every model
  download blocks for the full TCP timeout while `curl` on the same box succeeds
  in 80 ms. Diagnosed from `ss -tnp` showing `SYN-SENT` to a v6 address.
- `httpx.ASGITransport` delivers a response body as one chunk. Two tap tests about
  *when* bytes arrive passed vacuously against it. Rewritten to run both the tap
  and its fake upstream on real loopback sockets, which is what production does.
- Ports 8080, 8765 and 3000 were all taken by other users on this shared
  workstation. Hence the 18xxx/19xxx block.
