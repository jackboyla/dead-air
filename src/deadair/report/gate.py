"""Latency budgets: turn a run into pass or fail.

A budget is a one-line spec, so it fits on a command line or in a CI step::

    perceived_ttfa:p95<=1200     95th percentile of perceived first audio, in ms
    llm_ttft<=300                p95 is the default statistic
    barge_in:max<=600            mean, max, p50, p90, p95 and p99 are accepted
    completion_rate>=0.99        share of measured turns that completed
    protocol_violations<=0       count of contract breaks across measured turns
    rejected_sessions<=0         sessions refused for lack of capacity

A budget on a latency nobody measured fails. "No data" must not read as "within
budget", or a run that never produced audio would pass every latency gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from deadair.probe.runner import RunResult
from deadair.report.stats import HEADLINE, STAGES, TurnStats, summarize

_SPEC = re.compile(r"^(?P<metric>[a-z_]+)(?::(?P<stat>p\d{1,2}|mean|max))?\s*(?P<op><=|>=)\s*(?P<limit>\d+(?:\.\d+)?)$")

LATENCY_METRICS = frozenset(name for name, _, _ in STAGES + HEADLINE) | {"realtime_factor"}
RUN_METRICS = frozenset({"completion_rate", "protocol_violations", "rejected_sessions"})


class BudgetSpecError(ValueError):
    pass


@dataclass(frozen=True)
class Budget:
    metric: str
    stat: str | None
    op: str
    limit: float

    @property
    def text(self) -> str:
        stat = f":{self.stat}" if self.stat else ""
        return f"{self.metric}{stat}{self.op}{self.limit:g}"


@dataclass(frozen=True)
class BudgetResult:
    budget: Budget
    actual: float | None

    @property
    def passed(self) -> bool:
        if self.actual is None:
            return False
        if self.budget.op == "<=":
            return self.actual <= self.budget.limit
        return self.actual >= self.budget.limit


def parse_budget(text: str) -> Budget:
    match = _SPEC.match(text.replace(" ", ""))
    if match is None:
        raise BudgetSpecError(f"cannot parse budget {text!r}; expected e.g. perceived_ttfa:p95<=1200")
    metric = match["metric"]
    stat = match["stat"]
    if metric in LATENCY_METRICS:
        stat = stat or "p95"
        if stat.startswith("p") and int(stat[1:]) not in (50, 90, 95, 99):
            raise BudgetSpecError(f"{text!r}: percentile must be p50, p90, p95 or p99")
    elif metric in RUN_METRICS:
        if stat:
            raise BudgetSpecError(f"{text!r}: {metric} takes no statistic")
    else:
        known = ", ".join(sorted(LATENCY_METRICS | RUN_METRICS))
        raise BudgetSpecError(f"{text!r}: unknown metric {metric}. Known: {known}")
    return Budget(metric=metric, stat=stat, op=match["op"], limit=float(match["limit"]))


def parse_budgets(texts: list[str]) -> list[Budget]:
    return [parse_budget(text) for text in texts]


def evaluate(result: RunResult, budgets: list[Budget]) -> list[BudgetResult]:
    stats = summarize(result.measured_turns)
    return [BudgetResult(budget=budget, actual=_actual(result, stats, budget)) for budget in budgets]


def _actual(result: RunResult, stats: TurnStats, budget: Budget) -> float | None:
    if budget.metric == "completion_rate":
        return stats.completion_rate if stats.total_turns else None
    if budget.metric == "protocol_violations":
        return float(stats.protocol_violations)
    if budget.metric == "rejected_sessions":
        return float(result.rejected_sessions)
    distribution = stats.realtime_factor if budget.metric == "realtime_factor" else stats.stage(budget.metric)
    if distribution is None:
        return None
    if budget.stat == "mean":
        return distribution.mean
    if budget.stat == "max":
        return distribution.maximum
    assert budget.stat is not None
    return distribution.percentiles[int(budget.stat[1:])]


def render_gate(results: list[BudgetResult]) -> str:
    lines = ["| Budget | Actual | Result |", "|---|---:|:---:|"]
    for item in results:
        actual = "no data" if item.actual is None else f"{item.actual:.4g}"
        lines.append(f"| `{item.budget.text}` | {actual} | {'pass' if item.passed else '**FAIL**'} |")
    return "\n".join(lines) + "\n"
