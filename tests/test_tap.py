"""Tests for the measuring LLM proxy.

The two properties worth defending are that the tap does not change what the
client sees, and that it does not change *when* the client sees it. A proxy that
buffered a stream would still pass a correctness test while destroying the
latency it was installed to measure, so streaming is asserted on timing, not only
on content.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from deadair.tap.faults import FaultInjector, FaultSpec, FaultSpecError, parse_specs
from deadair.tap.proxy import LLMTap, TapConfig

CHUNK_DELAY_S = 0.02
CHUNK_COUNT = 6


def build_upstream() -> Starlette:
    """A stand-in LLM server that streams SSE chunks on a known schedule."""

    async def chat(request):
        body = await request.json()
        if not body.get("stream"):
            return JSONResponse({"choices": [{"message": {"content": "hello"}}], "usage": {"completion_tokens": 5}})

        async def stream():
            for index in range(CHUNK_COUNT):
                await asyncio.sleep(CHUNK_DELAY_S)
                yield f'data: {{"choices":[{{"delta":{{"content":"tok{index}"}}}}]}}\n\n'.encode()
            yield b'data: {"usage":{"prompt_tokens":11,"completion_tokens":6}}\n\n'
            yield b"data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def slow_headers(request):
        await asyncio.sleep(0.15)
        return JSONResponse({"ok": True})

    return Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"]), Route("/v1/slow", slow_headers)])


class LiveServer:
    """A uvicorn server on an ephemeral port, started and stopped per test.

    Real sockets rather than an in-memory ASGI transport: ``httpx.ASGITransport``
    delivers a response body as a single chunk, which would silently defeat every
    assertion here about *when* bytes arrive. Chunk boundaries are the measurement,
    so the tests have to run over the same transport production does.
    """

    def __init__(self, app) -> None:
        self.app = app
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self.port = 0

    async def start(self) -> "LiveServer":
        config = uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.01)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


class Harness:
    """An upstream and a tap, both listening on real loopback sockets."""

    def __init__(self, upstream: LiveServer, tap: LLMTap, tap_server: LiveServer) -> None:
        self.upstream = upstream
        self.tap = tap
        self.tap_server = tap_server

    @classmethod
    async def start(cls, **overrides) -> "Harness":
        upstream = await LiveServer(build_upstream()).start()
        tap = LLMTap(TapConfig(upstream=upstream.url, **overrides))
        tap_server = await LiveServer(tap.build_app()).start()
        return cls(upstream, tap, tap_server)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.tap_server.url, timeout=30.0)

    async def stop(self) -> None:
        await self.tap_server.stop()
        await self.upstream.stop()


@pytest.fixture
async def harness(tmp_path: Path):
    h = await Harness.start(trace_path=tmp_path / "tap.jsonl")
    yield h
    await h.stop()


class TestPassthrough:
    async def test_non_streaming_body_is_unchanged(self, harness: Harness) -> None:
        async with harness.client() as client:
            response = await client.post("/v1/chat/completions", json={"model": "m", "messages": [], "stream": False})
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "hello"

    async def test_streamed_chunks_arrive_as_they_are_produced(self, harness: Harness) -> None:
        """The tap must relay chunk by chunk, not accumulate and flush."""

        arrivals: list[float] = []
        async with harness.client() as client:
            async with client.stream("POST", "/v1/chat/completions", json={"model": "m", "stream": True}) as response:
                async for _ in response.aiter_bytes():
                    arrivals.append(time.monotonic())

        assert len(arrivals) >= CHUNK_COUNT
        # A buffering proxy delivers everything at once; a streaming one spreads
        # arrivals over at least the upstream's own production time.
        assert arrivals[-1] - arrivals[0] > CHUNK_DELAY_S * (CHUNK_COUNT - 2)

    async def test_request_id_header_is_added_for_correlation(self, harness: Harness) -> None:
        async with harness.client() as client:
            response = await client.post("/v1/chat/completions", json={"model": "m", "stream": False})
        assert response.headers["x-deadair-request-id"]


class TestMeasurement:
    async def test_latencies_are_recorded_and_ordered(self, harness: Harness) -> None:
        async with harness.client() as client:
            async with client.stream("POST", "/v1/chat/completions", json={"model": "m", "stream": True}) as response:
                async for _ in response.aiter_bytes():
                    pass

        record = harness.tap.records[-1]
        assert record.status == 200
        assert record.stream is True
        assert record.model == "m"
        assert record.ttft_ms is not None and record.total_ms is not None
        assert record.ttft_ms <= record.total_ms
        assert record.queue_ms is not None and record.queue_ms <= record.ttft_ms

    async def test_usage_is_scraped_from_the_stream(self, harness: Harness) -> None:
        async with harness.client() as client:
            async with client.stream("POST", "/v1/chat/completions", json={"model": "m", "stream": True}) as response:
                async for _ in response.aiter_bytes():
                    pass

        record = harness.tap.records[-1]
        assert record.prompt_tokens == 11
        assert record.completion_tokens == 6

    async def test_queue_time_grows_when_the_upstream_is_slow_to_answer(self, harness: Harness) -> None:
        async with harness.client() as client:
            await client.get("/v1/slow")
        record = harness.tap.records[-1]
        assert record.queue_ms is not None and record.queue_ms >= 140

    async def test_trace_line_is_written_per_request(self, harness: Harness, tmp_path: Path) -> None:
        async with harness.client() as client:
            await client.post("/v1/chat/completions", json={"model": "m", "stream": False})
        await harness.stop()

        lines = (tmp_path / "tap.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["path"] == "/v1/chat/completions"
        assert row["latency_ms"]["total"] is not None

    async def test_metrics_render_in_prometheus_format(self, harness: Harness) -> None:
        async with harness.client() as client:
            await client.post("/v1/chat/completions", json={"model": "m", "stream": False})
            response = await client.get("/metrics")
        assert "deadair_llm_requests_total" in response.text
        assert "deadair_llm_ttft_seconds" in response.text

    async def test_scrape_body_matches_its_declared_content_type(self, harness: Harness) -> None:
        """Prometheus rejects the whole scrape when these disagree.

        Plain-text exposition served as OpenMetrics fails with "data does not end
        with # EOF", because OpenMetrics requires that trailer and the plain-text
        generator does not write it. The failure is silent from the tap's side:
        the endpoint returns 200 and the target shows down.
        """

        async with harness.client() as client:
            response = await client.get("/metrics")

        content_type = response.headers["content-type"]
        if "openmetrics" in content_type:
            assert response.text.endswith("# EOF\n")
        else:
            assert "text/plain" in content_type
            assert not response.text.endswith("# EOF\n")


class TestFaultSpecs:
    @pytest.mark.parametrize(
        ("text", "kind"),
        [
            ("slow-start:400ms", "slow-start"),
            ("slow-start:0.2-1.5s", "slow-start"),
            ("stall:250ms@3", "stall"),
            ("truncate@7", "truncate"),
            ("reject:503", "reject"),
            ("reject:500;p=0.25", "reject"),
        ],
    )
    def test_valid_specs_parse(self, text: str, kind: str) -> None:
        assert FaultSpec.parse(text).kind == kind

    @pytest.mark.parametrize(
        "text",
        ["", "explode", "slow-start", "truncate", "stall:abc@1", "reject:503;p=2", "slow-start:9-1s"],
    )
    def test_invalid_specs_are_rejected_with_a_useful_error(self, text: str) -> None:
        with pytest.raises(FaultSpecError):
            FaultSpec.parse(text)

    def test_duration_ranges_are_sampled_within_bounds(self) -> None:
        import random

        spec = FaultSpec.parse("slow-start:200-400ms")
        rng = random.Random(0)
        for _ in range(50):
            assert 0.2 <= spec.sample_delay(rng) <= 0.4

    def test_probability_gates_application(self) -> None:
        import random

        injector = FaultInjector(parse_specs(["reject:503;p=0.0"]), rng=random.Random(1))
        assert injector.decide().reject is None

        injector = FaultInjector(parse_specs(["reject:503;p=1.0"]), rng=random.Random(1))
        assert injector.decide().reject == 503

    def test_a_decision_is_drawn_once_per_request(self) -> None:
        """Stall settings must not flicker partway through one response."""

        import random

        injector = FaultInjector(parse_specs(["stall:100-900ms@2"]), rng=random.Random(7))
        decision = injector.decide()
        first = decision.stall_for(2)
        second = decision.stall_for(2)
        assert decision.stall_s > 0
        assert first is not None and second is not None
        first.close()
        second.close()


class TestFaultInjection:
    async def test_reject_answers_without_contacting_upstream(self, tmp_path: Path) -> None:
        harness = await Harness.start(faults=parse_specs(["reject:503"]))
        try:
            async with harness.client() as client:
                response = await client.post("/v1/chat/completions", json={"model": "m"})
            assert response.status_code == 503
            record = harness.tap.records[-1]
            assert record.error == "fault:reject"
            # Never reached the upstream, so there is no connect timestamp.
            assert record.connected_at is None
        finally:
            await harness.stop()

    async def test_slow_start_delays_the_first_byte(self, tmp_path: Path) -> None:
        harness = await Harness.start(faults=parse_specs(["slow-start:200ms"]))
        try:
            started = time.monotonic()
            async with harness.client() as client:
                await client.post("/v1/chat/completions", json={"model": "m", "stream": False})
            assert time.monotonic() - started >= 0.2
            assert harness.tap.records[-1].faults == ["slow-start:200ms"]
        finally:
            await harness.stop()

    async def test_truncate_cuts_the_stream_short(self, tmp_path: Path) -> None:
        harness = await Harness.start(faults=parse_specs(["truncate@2"]))
        try:
            chunks = []
            async with harness.client() as client:
                async with client.stream(
                    "POST", "/v1/chat/completions", json={"model": "m", "stream": True}
                ) as response:
                    async for chunk in response.aiter_bytes():
                        chunks.append(chunk)
            assert len(chunks) == 2
            assert harness.tap.records[-1].error == "fault:truncate"
        finally:
            await harness.stop()

    async def test_faults_can_be_replaced_while_running(self, tmp_path: Path) -> None:
        """Degrading a live backend must not require a restart."""

        harness = await Harness.start()
        try:
            async with harness.client() as client:
                assert (await client.get("/faults")).json()["faults"] == []
                await client.post("/faults", json={"faults": ["reject:503"]})
                assert (await client.post("/v1/chat/completions", json={"model": "m"})).status_code == 503
                await client.post("/faults", json={"faults": []})
                assert (await client.post("/v1/chat/completions", json={"model": "m"})).status_code == 200
        finally:
            await harness.stop()


class TestUpstreamFailure:
    async def test_unreachable_upstream_becomes_a_502_not_a_crash(self, tmp_path: Path) -> None:
        tap = LLMTap(TapConfig(upstream="http://127.0.0.1:1", connect_timeout_s=0.25))
        server = await LiveServer(tap.build_app()).start()
        try:
            async with httpx.AsyncClient(base_url=server.url, timeout=10.0) as client:
                response = await client.post("/v1/chat/completions", json={"model": "m"})
            assert response.status_code == 502
            assert response.json()["error"]["type"] == "upstream_unavailable"
            assert tap.records[-1].error
        finally:
            await server.stop()
