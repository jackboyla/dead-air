"""Summary statistics for a measurement run.

Two choices here are deliberate and worth stating, because they change the
numbers a reader sees.

**Percentiles are computed by nearest rank on the observed samples, not by
interpolation.** Every reported percentile is therefore a latency that actually
occurred. Interpolated percentiles invent values between samples, which is
harmless with a million requests and misleading with sixty turns.

**The stage breakdown is reported as medians that do not sum to the median
total.** The temptation is to normalise them so they add up. Resisting it is the
point: the slowest ASR turn and the slowest TTS turn are usually different turns,
and a budget forced to reconcile hides that. Where a single additive view is
needed, ``budget`` decomposes the *same* turn each time.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Callable

from localvoice.timeline import TurnTimeline

# Ordered stages of the pipeline as the client observes them. The labels are the
# vocabulary used by the report, the dashboard, and the README, so they are
# defined once here.
STAGES: tuple[tuple[str, str, Callable[[TurnTimeline], float | None]], ...] = (
    ("vad_eou_lag", "VAD end-of-turn", lambda t: t.vad_eou_lag_ms),
    ("asr", "ASR", lambda t: t.asr_ms),
    ("llm_ttft", "LLM time to first token", lambda t: t.llm_ttft_ms),
    ("tts_ttfb", "TTS time to first byte", lambda t: t.tts_ttfb_ms),
)

HEADLINE: tuple[tuple[str, str, Callable[[TurnTimeline], float | None]], ...] = (
    ("ttfa", "Time to first audio (server clock)", lambda t: t.ttfa_ms),
    ("perceived_ttfa", "Time to first audio (user clock)", lambda t: t.perceived_ttfa_ms),
    ("response_total", "Full response", lambda t: t.response_total_ms),
    ("barge_in", "Barge-in to last audio", lambda t: t.barge_in_ms),
)

DEFAULT_PERCENTILES = (50, 90, 95, 99)


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile. ``values`` need not be sorted."""

    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    if q <= 0:
        return ordered[0]
    if q >= 100:
        return ordered[-1]
    rank = math.ceil(q / 100.0 * len(ordered))
    return ordered[min(max(rank, 1), len(ordered)) - 1]


@dataclass
class Distribution:
    """The shape of one measured quantity."""

    name: str
    label: str
    unit: str
    count: int
    mean: float
    minimum: float
    maximum: float
    percentiles: dict[int, float]

    @property
    def p50(self) -> float:
        return self.percentiles[50]

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "unit": self.unit,
            "count": self.count,
            "mean": round(self.mean, 1),
            "min": round(self.minimum, 1),
            "max": round(self.maximum, 1),
            "percentiles": {str(q): round(v, 1) for q, v in self.percentiles.items()},
        }


def describe(
    name: str,
    label: str,
    values: Iterable[float | None],
    unit: str = "ms",
    percentiles: Sequence[int] = DEFAULT_PERCENTILES,
) -> Distribution | None:
    """Summarize a sample, or return None when nothing was measured."""

    sample = [float(value) for value in values if value is not None and math.isfinite(value)]
    if not sample:
        return None
    return Distribution(
        name=name,
        label=label,
        unit=unit,
        count=len(sample),
        mean=sum(sample) / len(sample),
        minimum=min(sample),
        maximum=max(sample),
        percentiles={q: percentile(sample, q) for q in percentiles},
    )


@dataclass
class TurnStats:
    """Everything the report needs about one run's turns."""

    total_turns: int
    completed_turns: int
    failed_turns: int
    timed_out_turns: int
    cancelled_turns: int
    stages: list[Distribution]
    headline: list[Distribution]
    realtime_factor: Distribution | None
    error_counts: dict[str, int]

    @property
    def completion_rate(self) -> float:
        if self.total_turns == 0:
            return 0.0
        return self.completed_turns / self.total_turns

    def stage(self, name: str) -> Distribution | None:
        for distribution in self.stages + self.headline:
            if distribution.name == name:
                return distribution
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "total_turns": self.total_turns,
            "completed_turns": self.completed_turns,
            "failed_turns": self.failed_turns,
            "timed_out_turns": self.timed_out_turns,
            "cancelled_turns": self.cancelled_turns,
            "completion_rate": round(self.completion_rate, 4),
            "error_counts": self.error_counts,
            "stages": [d.to_json() for d in self.stages],
            "headline": [d.to_json() for d in self.headline],
            "realtime_factor": self.realtime_factor.to_json() if self.realtime_factor else None,
        }


def summarize(turns: Sequence[TurnTimeline]) -> TurnStats:
    error_counts: dict[str, int] = {}
    for turn in turns:
        for error in turn.errors:
            error_counts[error] = error_counts.get(error, 0) + 1

    stages = [
        distribution
        for name, label, getter in STAGES
        if (distribution := describe(name, label, (getter(turn) for turn in turns))) is not None
    ]
    headline = [
        distribution
        for name, label, getter in HEADLINE
        if (distribution := describe(name, label, (getter(turn) for turn in turns))) is not None
    ]

    return TurnStats(
        total_turns=len(turns),
        completed_turns=sum(1 for turn in turns if turn.completed),
        failed_turns=sum(1 for turn in turns if turn.status == "failed"),
        timed_out_turns=sum(1 for turn in turns if "turn_timeout" in turn.errors),
        cancelled_turns=sum(1 for turn in turns if turn.status == "cancelled"),
        stages=stages,
        headline=headline,
        realtime_factor=describe(
            "realtime_factor",
            "Realtime factor",
            (turn.realtime_factor for turn in turns),
            unit="x",
        ),
        error_counts=error_counts,
    )


@dataclass
class BudgetSlice:
    """One stage's share of a single turn's time to first audio."""

    name: str
    label: str
    ms: float
    share: float


def budget(turn: TurnTimeline) -> list[BudgetSlice]:
    """Decompose one turn's perceived latency into consecutive, additive stages.

    Uses a single turn so the parts genuinely sum. Pick the turn with
    :func:`representative_turn` rather than averaging across turns.
    """

    parts = [(name, label, getter(turn)) for name, label, getter in STAGES]
    measured = [(name, label, value) for name, label, value in parts if value is not None]
    total = sum(value for _, _, value in measured)
    if total <= 0:
        return []
    return [BudgetSlice(name=name, label=label, ms=value, share=value / total) for name, label, value in measured]


def representative_turn(turns: Sequence[TurnTimeline]) -> TurnTimeline | None:
    """The completed turn whose perceived latency is closest to the median.

    A real turn rather than a synthetic average, so its stage breakdown is
    internally consistent and can be traced back to a line in the event log.
    """

    candidates = [turn for turn in turns if turn.completed and turn.perceived_ttfa_ms is not None]
    if not candidates:
        return None
    target = percentile([turn.perceived_ttfa_ms for turn in candidates if turn.perceived_ttfa_ms is not None], 50)
    return min(candidates, key=lambda turn: abs((turn.perceived_ttfa_ms or 0.0) - target))
