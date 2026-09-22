"""A streaming reverse proxy that measures, and optionally breaks, the LLM backend.

The Realtime event stream tells a client when the *pipeline* produced its first
token. It cannot say how much of that was the language model and how much was the
pipeline deciding to call it. Sitting between ``speech-to-speech`` and the LLM
server answers that, and does it without forking either one.

Two jobs, one process:

**Measure.** Per request: queue wait (accepted to upstream connected), time to
first token, inter-token gaps, total, and token counts. Exported as Prometheus
histograms and appended to a JSONL trace for offline joining against turn timelines.

**Break.** Fault injection is a first-class feature rather than a test hook,
because "what happens when the LLM stalls mid-sentence" is a question every voice
deployment eventually has to answer, and the honest way to answer it is to make it
happen on demand. See :mod:`localvoice.tap.faults`.

Streaming bodies are forwarded chunk by chunk and never buffered whole. A proxy
that accumulates an SSE stream before relaying it would destroy the very latency
it was installed to measure.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from localvoice.tap.faults import FaultInjector, FaultSpec
from localvoice.tap.metrics import TapMetrics

logger = logging.getLogger("localvoice.tap")

# Hop-by-hop headers must not be forwarded; length and encoding are recomputed by
# the server for the response we actually emit.
_STRIPPED_REQUEST_HEADERS = frozenset({"host", "content-length", "connection", "keep-alive", "transfer-encoding"})
_STRIPPED_RESPONSE_HEADERS = frozenset({"content-length", "content-encoding", "transfer-encoding", "connection"})


@dataclass
class RequestRecord:
    """Everything the tap observed about one upstream call."""

    request_id: str
    path: str
    model: str | None
    stream: bool
    accepted_at: float
    wall_at: float
    connected_at: float | None = None
    first_token_at: float | None = None
    finished_at: float | None = None
    status: int | None = None
    token_times: list[float] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None
    faults: list[str] = field(default_factory=list)

    def _ms(self, later: float | None, earlier: float | None) -> float | None:
        if later is None or earlier is None:
            return None
        return (later - earlier) * 1000.0

    @property
    def queue_ms(self) -> float | None:
        """Time between accepting the request and the upstream responding.

        With llama.cpp this is slot-acquisition plus prompt processing, which is
        exactly the term that grows when concurrent sessions exceed ``-np``.
        """

        return self._ms(self.connected_at, self.accepted_at)

    @property
    def ttft_ms(self) -> float | None:
        return self._ms(self.first_token_at, self.accepted_at)

    @property
    def total_ms(self) -> float | None:
        return self._ms(self.finished_at, self.accepted_at)

    @property
    def decode_ms(self) -> float | None:
        return self._ms(self.finished_at, self.first_token_at)

    @property
    def tokens_per_s(self) -> float | None:
        decode = self.decode_ms
        if decode is None or decode <= 0 or len(self.token_times) < 2:
            return None
        return (len(self.token_times) - 1) / (decode / 1000.0)

    @property
    def max_inter_token_ms(self) -> float | None:
        """Largest gap between consecutive tokens.

        A healthy mean with a large maximum is the signature of a stall, and a
        stall mid-utterance is audible where a slightly slower mean is not.
        """

        if len(self.token_times) < 2:
            return None
        return max((b - a) * 1000.0 for a, b in zip(self.token_times, self.token_times[1:]))

    def to_json(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "path": self.path,
            "model": self.model,
            "stream": self.stream,
            "wall": self.wall_at,
            "status": self.status,
            "error": self.error,
            "faults": self.faults,
            "tokens": {
                "prompt": self.prompt_tokens,
                "completion": self.completion_tokens,
                "observed_chunks": len(self.token_times),
            },
            "latency_ms": {
                "queue": self.queue_ms,
                "ttft": self.ttft_ms,
                "decode": self.decode_ms,
                "total": self.total_ms,
                "max_inter_token": self.max_inter_token_ms,
            },
            "tokens_per_s": self.tokens_per_s,
        }


class TraceWriter:
    """Append-only JSONL sink. One line per upstream request."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._handle: TextIO | None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("a", encoding="utf-8")
        else:
            self._handle = None

    def write(self, record: RequestRecord) -> None:
        if self._handle is None:
            return
        self._handle.write(json.dumps(record.to_json()) + "\n")
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


@dataclass
class TapConfig:
    upstream: str = "http://127.0.0.1:18080"
    host: str = "127.0.0.1"
    port: int = 18900
    trace_path: Path | None = None
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 300.0
    faults: list[FaultSpec] = field(default_factory=list)


class LLMTap:
    """ASGI application proxying an OpenAI-compatible server while timing it."""

    def __init__(self, config: TapConfig) -> None:
        self.config = config
        self.metrics = TapMetrics()
        self.faults = FaultInjector(config.faults)
        self.trace = TraceWriter(config.trace_path)
        self.records: list[RequestRecord] = []
        self._client: httpx.AsyncClient | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def startup(self) -> None:
        timeout = httpx.Timeout(
            self.config.read_timeout_s,
            connect=self.config.connect_timeout_s,
        )
        # No connection cap: the tap must never become the bottleneck it measures.
        limits = httpx.Limits(max_connections=None, max_keepalive_connections=64)
        self._client = httpx.AsyncClient(base_url=self.config.upstream, timeout=timeout, limits=limits)
        logger.info("tap upstream=%s faults=%s", self.config.upstream, self.faults.describe())

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self.trace.close()

    # ── Routes ───────────────────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def lifespan(self, app: Starlette) -> AsyncIterator[None]:
        await self.startup()
        try:
            yield
        finally:
            await self.shutdown()

    def build_app(self) -> Starlette:
        routes = [
            Route("/healthz", self.healthz, methods=["GET"]),
            Route("/metrics", self.metrics_endpoint, methods=["GET"]),
            Route("/stats", self.stats_endpoint, methods=["GET"]),
            Route("/faults", self.faults_endpoint, methods=["GET", "POST"]),
            Route("/{path:path}", self.proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH"]),
        ]
        app = Starlette(routes=routes, lifespan=self.lifespan)
        app.state.tap = self
        return app

    async def healthz(self, request: Request) -> Response:
        return JSONResponse({"status": "ok", "upstream": self.config.upstream})

    async def metrics_endpoint(self, request: Request) -> Response:
        body, content_type = self.metrics.render()
        return PlainTextResponse(body, media_type=content_type)

    async def stats_endpoint(self, request: Request) -> Response:
        return JSONResponse(
            {
                "requests": len(self.records),
                "faults": self.faults.describe(),
                "recent": [record.to_json() for record in self.records[-20:]],
            }
        )

    async def faults_endpoint(self, request: Request) -> Response:
        """Read or replace the active fault set without restarting the stack.

        Live reconfiguration matters because the interesting experiment is
        "degrade the backend mid-conversation and listen to what happens", which a
        restart would destroy along with the session.
        """

        if request.method == "POST":
            body = await request.json()
            specs = [FaultSpec.parse(item) for item in body.get("faults", [])]
            self.faults.replace(specs)
            logger.info("faults updated: %s", self.faults.describe())
        return JSONResponse({"faults": self.faults.describe()})

    # ── Proxy ────────────────────────────────────────────────────────────

    async def proxy(self, request: Request) -> Response:
        assert self._client is not None, "tap used before startup"
        body = await request.body()
        record = RequestRecord(
            request_id=uuid.uuid4().hex[:12],
            path=request.url.path,
            model=_model_of(body),
            stream=_is_stream(body),
            accepted_at=time.monotonic(),
            wall_at=time.time(),
        )
        self.records.append(record)
        self.metrics.inflight.inc()

        decision = self.faults.decide()
        record.faults = decision.names

        if decision.reject is not None:
            await decision.apply_pre_delay()
            record.status = decision.reject
            record.finished_at = time.monotonic()
            record.error = "fault:reject"
            self._finish(record)
            return JSONResponse(
                {"error": {"message": "injected upstream failure", "type": "server_error"}},
                status_code=decision.reject,
            )

        await decision.apply_pre_delay()

        headers = {k: v for k, v in request.headers.items() if k.lower() not in _STRIPPED_REQUEST_HEADERS}
        upstream_request = self._client.build_request(
            request.method,
            request.url.path,
            params=request.url.query.encode("ascii") or None,
            headers=headers,
            content=body,
        )

        try:
            response = await self._client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            record.status = 502
            record.finished_at = time.monotonic()
            self._finish(record)
            logger.warning("upstream error id=%s %s", record.request_id, record.error)
            return JSONResponse(
                {"error": {"message": str(exc), "type": "upstream_unavailable"}},
                status_code=502,
            )

        record.connected_at = time.monotonic()
        record.status = response.status_code
        out_headers = {k: v for k, v in response.headers.items() if k.lower() not in _STRIPPED_RESPONSE_HEADERS}
        out_headers["x-localvoice-request-id"] = record.request_id

        return StreamingResponse(
            self._relay(response, record, decision),
            status_code=response.status_code,
            headers=out_headers,
            media_type=response.headers.get("content-type"),
        )

    async def _relay(
        self,
        response: httpx.Response,
        record: RequestRecord,
        decision: Any,
    ) -> AsyncIterator[bytes]:
        """Forward the upstream body, timing every chunk on the way through."""

        chunk_index = 0
        try:
            async for chunk in response.aiter_bytes():
                now = time.monotonic()
                if record.first_token_at is None:
                    record.first_token_at = now
                    self.metrics.observe_ttft(record)
                record.token_times.append(now)
                _scrape_usage(chunk, record)

                stall = decision.stall_for(chunk_index)
                if stall:
                    await stall
                if decision.truncate_at(chunk_index):
                    record.error = "fault:truncate"
                    logger.info("truncating stream id=%s at chunk %d", record.request_id, chunk_index)
                    break

                chunk_index += 1
                yield chunk
        except httpx.HTTPError as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            logger.warning("stream broken id=%s %s", record.request_id, record.error)
        finally:
            await response.aclose()
            record.finished_at = time.monotonic()
            self._finish(record)

    def _finish(self, record: RequestRecord) -> None:
        self.metrics.inflight.dec()
        self.metrics.observe_request(record)
        self.trace.write(record)
        logger.debug(
            "id=%s status=%s queue=%s ttft=%s total=%s",
            record.request_id,
            record.status,
            _fmt(record.queue_ms),
            _fmt(record.ttft_ms),
            _fmt(record.total_ms),
        )


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}ms"


def _is_stream(body: bytes) -> bool:
    payload = _json_or_none(body)
    return bool(payload and payload.get("stream"))


def _model_of(body: bytes) -> str | None:
    payload = _json_or_none(body)
    if not payload:
        return None
    model = payload.get("model")
    return model if isinstance(model, str) else None


def _json_or_none(body: bytes) -> dict[str, Any] | None:
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _scrape_usage(chunk: bytes, record: RequestRecord) -> None:
    """Pull token counts out of a chunk if it happens to carry a usage block.

    Best effort by design. Usage may arrive in a final SSE frame, in a non-streamed
    body, or not at all, and a chunk boundary can split any of them. Missing counts
    are reported as missing rather than guessed.
    """

    if b"usage" not in chunk:
        return
    for line in chunk.split(b"\n"):
        line = line.strip()
        if line.startswith(b"data:"):
            line = line[5:].strip()
        if not line.startswith(b"{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            continue
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion = usage.get("completion_tokens", usage.get("output_tokens"))
        if isinstance(prompt, int):
            record.prompt_tokens = prompt
        if isinstance(completion, int):
            record.completion_tokens = completion


def build_app(config: TapConfig) -> Starlette:
    return LLMTap(config).build_app()
