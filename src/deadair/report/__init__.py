"""Turn measured runs into Markdown and a self-contained HTML report."""

from deadair.report.render import Report, render
from deadair.report.stats import Distribution, TurnStats, budget, percentile, summarize

__all__ = [
    "Distribution",
    "Report",
    "TurnStats",
    "budget",
    "percentile",
    "render",
    "summarize",
]
