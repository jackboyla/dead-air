"""Prometheus exporter for the speech-to-speech server's own counters.

The pipeline already publishes ``/v1/usage`` and ``/v1/pool``; it just publishes
them as JSON. This turns them into metrics so pool saturation can be watched on
the same dashboard as the latency it causes.

``/v1/pool`` is the interesting one. It distinguishes ``idle``, ``active``,
``draining`` and ``stuck``, and the last of those is the state that matters
operationally: a unit whose session was released but whose handler never drained
is occupied forever and silently reduces capacity. Counting them is how you notice
before users do.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

# Plain-text exposition; see the note in localvoice.tap.metrics about why this
# must not be paired with the OpenMetrics content type.
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, generate_latest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

logger = logging.getLogger("localvoice.exporter")

POOL_STATES = ("idle", "active", "draining", "stuck")


class PipelineExporter:
    """Polls a speech-to-speech server and republishes its counters."""

    def __init__(self, base_url: str = "http://127.0.0.1:18765", interval_s: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.interval_s = interval_s
        self.registry = CollectorRegistry()
        self._task: asyncio.Task[None] | None = None
        self._client: httpx.AsyncClient | None = None
        self.last_error: str | None = None

        self.pool_size = Gauge("s2s_pool_size", "Pipeline units in the pool.", registry=self.registry)
        self.pool_in_use = Gauge("s2s_pool_in_use", "Pipeline units bound to a session.", registry=self.registry)
        self.pool_state = Gauge("s2s_pool_units", "Pipeline units by state.", ["state"], registry=self.registry)
        self.connections = Gauge(
            "s2s_connections_total_reported", "Connections reported by the server.", registry=self.registry
        )
        self.turns = Gauge("s2s_turns_reported", "Turns reported by the server.", registry=self.registry)
        self.responses_completed = Gauge(
            "s2s_responses_completed_reported", "Completed responses reported by the server.", registry=self.registry
        )
        self.responses_cancelled = Gauge(
            "s2s_responses_cancelled_reported", "Cancelled responses reported by the server.", registry=self.registry
        )
        self.audio_seconds = Gauge(
            "s2s_output_audio_seconds_reported",
            "Synthesized audio seconds reported by the server.",
            registry=self.registry,
        )
        self.errors = Gauge(
            "s2s_errors_reported", "Errors reported by the server, by type.", ["type"], registry=self.registry
        )
        self.scrape_failures = Counter(
            "s2s_scrape_failures_total", "Failed scrapes of the pipeline server.", registry=self.registry
        )

    async def _poll_once(self) -> None:
        assert self._client is not None
        results: list[Any] = list(
            await asyncio.gather(
                self._get("/v1/usage"),
                self._get("/v1/pool"),
                return_exceptions=True,
            )
        )
        usage, pool = results[0], results[1]
        if isinstance(usage, dict):
            self._apply_usage(usage)
        if isinstance(pool, dict):
            self._apply_pool(pool)
        if isinstance(usage, BaseException) or isinstance(pool, BaseException):
            failure = usage if isinstance(usage, BaseException) else pool
            self.last_error = f"{type(failure).__name__}: {failure}"
            self.scrape_failures.inc()

    async def _get(self, path: str) -> dict[str, Any]:
        assert self._client is not None
        response = await self._client.get(f"{self.base_url}{path}")
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def _apply_usage(self, usage: dict[str, Any]) -> None:
        self.last_error = None
        for key, gauge in (
            ("connections", self.connections),
            ("turns", self.turns),
            ("responses_completed", self.responses_completed),
            ("responses_cancelled", self.responses_cancelled),
            ("audio_duration_s", self.audio_seconds),
        ):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                gauge.set(float(value))
        errors = usage.get("errors_by_type")
        if isinstance(errors, dict):
            for error_type, count in errors.items():
                if isinstance(count, (int, float)):
                    self.errors.labels(type=str(error_type)).set(float(count))

    def _apply_pool(self, pool: dict[str, Any]) -> None:
        size = pool.get("size")
        in_use = pool.get("in_use")
        if isinstance(size, (int, float)):
            self.pool_size.set(float(size))
        if isinstance(in_use, (int, float)):
            self.pool_in_use.set(float(in_use))
        counts = dict.fromkeys(POOL_STATES, 0)
        for unit in pool.get("units") or []:
            state = unit.get("state") if isinstance(unit, dict) else None
            if state in counts:
                counts[state] += 1
        # Every state is published every scrape, including the zeroes, so a state
        # that empties out shows as 0 rather than as a series that stops updating.
        for state, count in counts.items():
            self.pool_state.labels(state=state).set(float(count))

    async def _loop(self) -> None:
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the exporter must outlive its target
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.scrape_failures.inc()
                logger.debug("scrape failed: %s", self.last_error)
            await asyncio.sleep(self.interval_s)

    async def startup(self) -> None:
        self._client = httpx.AsyncClient(timeout=5.0)
        self._task = asyncio.create_task(self._loop(), name="localvoice-exporter")

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def build_app(self) -> Starlette:
        async def metrics(request: Request) -> Response:
            return PlainTextResponse(generate_latest(self.registry).decode("utf-8"), media_type=CONTENT_TYPE_LATEST)

        async def healthz(request: Request) -> Response:
            return JSONResponse({"status": "ok", "target": self.base_url, "last_error": self.last_error})

        @contextlib.asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncIterator[None]:
            await self.startup()
            try:
                yield
            finally:
                await self.shutdown()

        return Starlette(
            routes=[Route("/metrics", metrics), Route("/healthz", healthz)],
            lifespan=lifespan,
        )


def build_app(base_url: str, interval_s: float = 2.0) -> Starlette:
    return PipelineExporter(base_url, interval_s).build_app()
