"""Prometheus metrics for the LLM tap.

Bucket boundaries are chosen for speech, not for web serving. A voice turn is
unpleasant somewhere around 800 ms and broken well before 5 s, so the buckets are
dense where the decision lives and sparse in the tail. Default Prometheus buckets
would put six of their eleven boundaries above one second, where a voice agent has
already lost the conversation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Plain-text exposition and its matching content type. Pairing generate_latest
# with the OpenMetrics content type instead makes Prometheus reject every scrape
# with "data does not end with # EOF", because OpenMetrics requires that trailer.
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from localvoice.tap.proxy import RequestRecord

# Seconds. Dense through the range where a turn goes from good to bad.
LATENCY_BUCKETS = (
    0.025,
    0.05,
    0.075,
    0.1,
    0.15,
    0.2,
    0.3,
    0.4,
    0.5,
    0.75,
    1.0,
    1.5,
    2.0,
    3.0,
    5.0,
    10.0,
    30.0,
)

# Inter-token gaps have a much tighter useful range: past ~250 ms the speech
# synthesizer downstream has already run dry.
GAP_BUCKETS = (0.005, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.15, 0.25, 0.5, 1.0, 5.0)


class TapMetrics:
    """Metric set for one tap process.

    Holds its own registry rather than using the global default, so several taps
    can run in-process during tests without their counters colliding.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()

        self.requests = Counter(
            "localvoice_llm_requests_total",
            "Upstream LLM requests seen by the tap.",
            ["path", "status", "outcome"],
            registry=self.registry,
        )
        self.inflight = Gauge(
            "localvoice_llm_inflight_requests",
            "Requests accepted by the tap and not yet finished.",
            registry=self.registry,
        )
        self.queue_seconds = Histogram(
            "localvoice_llm_queue_seconds",
            "Accepted to upstream response headers. Slot wait plus prompt processing.",
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.ttft_seconds = Histogram(
            "localvoice_llm_ttft_seconds",
            "Accepted to first streamed chunk.",
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.total_seconds = Histogram(
            "localvoice_llm_request_seconds",
            "Accepted to last streamed chunk.",
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.max_gap_seconds = Histogram(
            "localvoice_llm_max_inter_token_seconds",
            "Largest gap between consecutive streamed chunks in a request.",
            buckets=GAP_BUCKETS,
            registry=self.registry,
        )
        self.output_tokens = Counter(
            "localvoice_llm_output_tokens_total",
            "Completion tokens reported by the upstream, when it reports them.",
            registry=self.registry,
        )
        self.faults = Counter(
            "localvoice_llm_faults_total",
            "Injected faults applied, by fault name.",
            ["fault"],
            registry=self.registry,
        )

    def observe_ttft(self, record: "RequestRecord") -> None:
        ttft = record.ttft_ms
        if ttft is not None:
            self.ttft_seconds.observe(ttft / 1000.0)

    def observe_request(self, record: "RequestRecord") -> None:
        outcome = "error" if record.error else "ok"
        self.requests.labels(
            path=record.path,
            status=str(record.status if record.status is not None else 0),
            outcome=outcome,
        ).inc()

        for name in record.faults:
            self.faults.labels(fault=name).inc()

        for value, histogram in (
            (record.queue_ms, self.queue_seconds),
            (record.total_ms, self.total_seconds),
            (record.max_inter_token_ms, self.max_gap_seconds),
        ):
            if value is not None:
                histogram.observe(value / 1000.0)

        if record.completion_tokens:
            self.output_tokens.inc(record.completion_tokens)

    def render(self) -> tuple[str, str]:
        return generate_latest(self.registry).decode("utf-8"), CONTENT_TYPE_LATEST
