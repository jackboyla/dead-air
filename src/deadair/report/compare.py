"""Compare two saved runs, stage by stage.

This is the regression check: run the same probe against two builds, then ask
which stage moved. A single end-to-end number says that something got slower;
the stage table says where.

Regressions are judged on the median, not the tail. With a few dozen turns a p95
is one or two samples, and flagging on it would mostly report noise. A change
must also clear an absolute floor as well as a relative one, so a 4 ms ASR stage
becoming 6 ms is not reported as a 50% regression.
"""

from __future__ import annotations

from dataclasses import dataclass

from deadair.probe.runner import RunResult
from deadair.report.stats import HEADLINE, STAGES, Distribution, TurnStats, summarize


@dataclass(frozen=True)
class MetricDelta:
    name: str
    label: str
    base: Distribution | None
    candidate: Distribution | None

    def delta(self, q: int) -> float | None:
        if self.base is None or self.candidate is None:
            return None
        return self.candidate.percentiles[q] - self.base.percentiles[q]

    def ratio(self, q: int) -> float | None:
        change = self.delta(q)
        if change is None or self.base is None or self.base.percentiles[q] <= 0:
            return None
        return change / self.base.percentiles[q]

    def regressed(self, threshold: float, floor_ms: float) -> bool:
        change, ratio = self.delta(50), self.ratio(50)
        return change is not None and ratio is not None and change > floor_ms and ratio > threshold


@dataclass(frozen=True)
class Comparison:
    base: RunResult
    candidate: RunResult
    base_stats: TurnStats
    candidate_stats: TurnStats
    metrics: list[MetricDelta]
    threshold: float
    floor_ms: float

    @property
    def regressions(self) -> list[str]:
        found = [m.name for m in self.metrics if m.regressed(self.threshold, self.floor_ms)]
        if self.candidate_stats.completion_rate < self.base_stats.completion_rate:
            found.append("completion_rate")
        if self.candidate_stats.protocol_violations > self.base_stats.protocol_violations:
            found.append("protocol_violations")
        return found


def compare(base: RunResult, candidate: RunResult, threshold: float = 0.10, floor_ms: float = 20.0) -> Comparison:
    base_stats = summarize(base.measured_turns)
    candidate_stats = summarize(candidate.measured_turns)
    metrics = [
        MetricDelta(name, label, base_stats.stage(name), candidate_stats.stage(name))
        for name, label, _ in HEADLINE + STAGES
    ]
    metrics = [m for m in metrics if m.base is not None or m.candidate is not None]
    return Comparison(base, candidate, base_stats, candidate_stats, metrics, threshold, floor_ms)


def _ms(distribution: Distribution | None, q: int) -> str:
    return "—" if distribution is None else f"{distribution.percentiles[q]:.0f} ms"


def _change(metric: MetricDelta, q: int) -> str:
    change, ratio = metric.delta(q), metric.ratio(q)
    if change is None:
        return "—"
    return f"{change:+.0f} ms" + (f" ({ratio:+.0%})" if ratio is not None else "")


def render_comparison(result: Comparison) -> str:
    base, candidate = result.base, result.candidate
    lines = [
        f"# {base.spec.name} → {candidate.spec.name}",
        "",
        f"Base `{base.started_at}` · candidate `{candidate.started_at}`.",
        f"A stage is flagged when its median rises by more than {result.threshold:.0%} "
        f"and more than {result.floor_ms:.0f} ms.",
        "",
        "| Measure | n | p50 base | p50 candidate | p50 change | p95 base | p95 candidate | p95 change |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for metric in result.metrics:
        n = f"{metric.base.count if metric.base else 0} / {metric.candidate.count if metric.candidate else 0}"
        flag = " ⚠" if metric.regressed(result.threshold, result.floor_ms) else ""
        lines.append(
            f"| {metric.label}{flag} | {n} | {_ms(metric.base, 50)} | {_ms(metric.candidate, 50)} | "
            f"{_change(metric, 50)} | {_ms(metric.base, 95)} | {_ms(metric.candidate, 95)} | {_change(metric, 95)} |"
        )
    b, c = result.base_stats, result.candidate_stats
    lines += [
        "",
        "| Outcome | Base | Candidate |",
        "|---|---:|---:|",
        f"| Completed | {b.completion_rate:.1%} of {b.total_turns} | {c.completion_rate:.1%} of {c.total_turns} |",
        f"| Protocol violations | {b.protocol_violations} | {c.protocol_violations} |",
        f"| Sessions refused | {base.rejected_sessions} | {candidate.rejected_sessions} |",
        "",
        f"**Regressions:** {', '.join(result.regressions) if result.regressions else 'none'}",
    ]
    return "\n".join(lines) + "\n"
