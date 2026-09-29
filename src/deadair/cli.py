"""Command line entry point.

::

    deadair tap       run the measuring, fault-injecting LLM proxy
    deadair exporter  republish the pipeline's own counters as Prometheus metrics
    deadair probe     drive Realtime sessions and record the latency budget
    deadair sweep     run a concurrency sweep and report where it breaks
    deadair report    re-render a saved run without re-running it
    deadair gate      check a saved run against latency budgets
    deadair compare   compare two saved runs stage by stage
    deadair mock      serve a deterministic Realtime target for tests and CI
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from deadair import __version__

DEFAULT_REALTIME_URL = "ws://127.0.0.1:18765/v1/realtime"
DEFAULT_TAP_URL = "http://127.0.0.1:18900"
DEFAULT_UPSTREAM = "http://127.0.0.1:18080"
DEFAULT_PIPELINE_URL = "http://127.0.0.1:18765"


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deadair",
        description="Measure a local OpenAI-Realtime voice deployment.",
    )
    parser.add_argument("--version", action="version", version=f"deadair {__version__}")
    parser.add_argument("--log-level", default="info", help="Logging level. Default is info.")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    tap = sub.add_parser("tap", help="Run the measuring LLM reverse proxy.")
    tap.add_argument("--upstream", default=DEFAULT_UPSTREAM, help="OpenAI-compatible server to proxy.")
    tap.add_argument("--host", default="127.0.0.1")
    tap.add_argument("--port", type=int, default=18900)
    tap.add_argument("--trace", type=Path, default=None, help="Append one JSON line per upstream request.")
    tap.add_argument(
        "--fault",
        action="append",
        default=[],
        metavar="SPEC",
        help="Fault to inject, repeatable. For example slow-start:800ms, stall:500ms@4, truncate@10, reject:503;p=0.2.",
    )

    exporter = sub.add_parser("exporter", help="Export the pipeline's /v1/usage and /v1/pool as metrics.")
    exporter.add_argument("--pipeline-url", default=DEFAULT_PIPELINE_URL)
    exporter.add_argument("--host", default="127.0.0.1")
    exporter.add_argument("--port", type=int, default=18901)
    exporter.add_argument("--interval", type=float, default=2.0, help="Scrape interval in seconds.")

    probe = sub.add_parser("probe", help="Drive Realtime sessions and record the latency budget.")
    _add_probe_arguments(probe)
    probe.add_argument("--concurrency", type=int, default=1, help="Simultaneous sessions.")
    probe.add_argument(
        "--barge-in-after",
        type=float,
        default=None,
        help="Interrupt this many seconds after the assistant starts speaking.",
    )
    probe.add_argument(
        "--fault",
        action="append",
        default=[],
        metavar="SPEC",
        help="Fault to apply at the tap for the duration of this run.",
    )
    _add_budget_argument(probe)

    sweep = sub.add_parser("sweep", help="Run the same scenario at increasing concurrency.")
    _add_probe_arguments(sweep)
    sweep.add_argument("--levels", default="1,2,4,8", help="Comma-separated concurrency levels. Default is 1,2,4,8.")

    report = sub.add_parser("report", help="Re-render a saved run.")
    report.add_argument("run", type=Path, help="Path to a run JSON produced by probe or sweep.")
    report.add_argument("--out", type=Path, default=None, help="Output directory. Defaults beside the input.")

    gate = sub.add_parser("gate", help="Check a saved run against latency budgets. Exits 2 on failure.")
    gate.add_argument("run", type=Path, help="Path to a run JSON produced by probe or sweep.")
    _add_budget_argument(gate, required=True)

    compare = sub.add_parser("compare", help="Compare two saved runs stage by stage.")
    compare.add_argument("base", type=Path, help="Run JSON to compare against.")
    compare.add_argument("candidate", type=Path, help="Run JSON under test.")
    compare.add_argument(
        "--threshold", type=float, default=0.10, help="Relative median rise that counts as a regression. Default 0.10."
    )
    compare.add_argument(
        "--floor-ms", type=float, default=20.0, help="Absolute median rise that must also be exceeded. Default 20."
    )
    compare.add_argument("--out", type=Path, default=None, help="Also write the comparison as Markdown here.")
    compare.add_argument("--fail-on-regression", action="store_true", help="Exit 2 when any regression is found.")

    mock = sub.add_parser("mock", help="Serve a deterministic Realtime target for tests and CI.")
    mock.add_argument("--host", default="127.0.0.1")
    mock.add_argument("--port", type=int, default=18766)
    mock.add_argument("--vad-silence-ms", type=int, default=200, help="Quiet time that ends a turn.")
    mock.add_argument("--stt-ms", type=int, default=40)
    mock.add_argument("--llm-ms", type=int, default=60)
    mock.add_argument("--tts-ms", type=int, default=30)
    mock.add_argument("--max-sessions", type=int, default=0, help="Refuse sessions past this many. 0 is unlimited.")
    mock.add_argument("--fail-every", type=int, default=0, help="Fail every Nth turn. 0 never fails.")
    mock.add_argument(
        "--stale-audio-after-cancel",
        action="store_true",
        help="Send audio after reporting a response cancelled, to exercise the violation detector.",
    )

    return parser


def _add_budget_argument(parser: argparse.ArgumentParser, required: bool = False) -> None:
    parser.add_argument(
        "--budget",
        action="append",
        default=[],
        required=required,
        metavar="SPEC",
        help="Budget to enforce, repeatable. For example perceived_ttfa:p95<=1200 or completion_rate>=0.99.",
    )


def _add_probe_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default=DEFAULT_REALTIME_URL, help="Realtime WebSocket endpoint.")
    parser.add_argument("--tap-url", default=DEFAULT_TAP_URL, help="Tap base URL, used for fault injection.")
    parser.add_argument(
        "--prompts",
        type=Path,
        default=Path("assets/prompts"),
        help="Prompt directory produced by scripts/make_prompts.py.",
    )
    parser.add_argument("--turns", type=int, default=6, help="Turns per session, warmup included.")
    parser.add_argument("--warmup", type=int, default=1, help="Leading turns excluded from statistics.")
    parser.add_argument(
        "--interval", type=float, default=0.0, help="Seconds between turn starts. 0 means back to back."
    )
    parser.add_argument("--send-rate", type=int, default=16000, choices=(16000, 24000))
    parser.add_argument("--instructions", default=None, help="Override the system instructions.")
    parser.add_argument("--voice", default=None, help="Requested output voice.")
    parser.add_argument("--name", default=None, help="Run name used in the report title.")
    parser.add_argument("--notes", default="", help="Free text recorded with the run.")
    parser.add_argument("--out", type=Path, default=Path("results/latest"), help="Output directory.")
    parser.add_argument("--no-trace", action="store_true", help="Skip writing raw event traces.")


def _session_config(args: argparse.Namespace) -> Any:
    from deadair.probe.client import SessionConfig

    config = SessionConfig(url=args.url, send_rate=args.send_rate)
    if args.instructions:
        config.instructions = args.instructions
    if args.voice:
        config.voice = args.voice
    return config


def _load_prompts(args: argparse.Namespace) -> Any:
    from deadair.probe.audio import PromptLibrary

    return PromptLibrary.load(args.prompts, target_rate=args.send_rate)


def _run_and_report(args: argparse.Namespace, spec: Any, stem: str) -> Any:
    from deadair.probe import runner
    from deadair.report.render import render

    prompts = _load_prompts(args)
    config = _session_config(args)
    out: Path = args.out
    trace_dir = None if args.no_trace else out / f"traces-{stem}"

    result = asyncio.run(runner.run(spec, config, prompts, trace_dir=trace_dir))
    runner.write_result(out / f"{stem}.json", result)
    report = render(result)
    markdown_path, html_path = report.write(out, stem=stem)
    print(report.markdown)
    print(f"Wrote {out / f'{stem}.json'}\n      {markdown_path}\n      {html_path}")
    return result


def _cmd_tap(args: argparse.Namespace) -> int:
    import uvicorn

    from deadair.tap.faults import FaultSpecError, parse_specs
    from deadair.tap.proxy import TapConfig, build_app

    try:
        faults = parse_specs(args.fault)
    except FaultSpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    app = build_app(
        TapConfig(
            upstream=args.upstream,
            host=args.host,
            port=args.port,
            trace_path=args.trace,
            faults=faults,
        )
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _cmd_exporter(args: argparse.Namespace) -> int:
    import uvicorn

    from deadair.exporter import build_app

    app = build_app(args.pipeline_url, interval_s=args.interval)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    from deadair.probe.runner import RunSpec

    name = args.name or (
        f"barge-in x{args.concurrency}" if args.barge_in_after is not None else f"latency x{args.concurrency}"
    )
    spec = RunSpec(
        name=name,
        concurrency=args.concurrency,
        turns=args.turns,
        warmup_turns=args.warmup,
        interval_s=args.interval,
        barge_in_after_s=args.barge_in_after,
        faults=list(args.fault),
        tap_url=args.tap_url,
        notes=args.notes,
    )
    from deadair.report.gate import BudgetSpecError, parse_budgets

    try:
        budgets = parse_budgets(args.budget)
    except BudgetSpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result = _run_and_report(args, spec, stem=f"probe-c{args.concurrency}")
    return _check_budgets(result, budgets)


def _check_budgets(result: Any, budgets: list[Any]) -> int:
    from deadair.report.gate import evaluate, render_gate

    if not budgets:
        return 0
    outcomes = evaluate(result, budgets)
    print(render_gate(outcomes))
    return 0 if all(outcome.passed for outcome in outcomes) else 2


def _cmd_sweep(args: argparse.Namespace) -> int:
    from deadair.probe.runner import RunSpec

    levels = [int(value) for value in str(args.levels).split(",") if value.strip()]
    summary: list[dict[str, Any]] = []
    for level in levels:
        spec = RunSpec(
            name=args.name or f"concurrency {level}",
            concurrency=level,
            turns=args.turns,
            warmup_turns=args.warmup,
            interval_s=args.interval,
            tap_url=args.tap_url,
            notes=args.notes,
        )
        result = _run_and_report(args, spec, stem=f"sweep-c{level}")
        summary.append(_sweep_row(level, result))

    _write_sweep_summary(args.out, summary)
    return 0


def _sweep_row(level: int, result: Any) -> dict[str, Any]:
    from deadair.report.stats import summarize

    stats = summarize(result.measured_turns)
    perceived = stats.stage("perceived_ttfa")
    return {
        "concurrency": level,
        "turns": stats.total_turns,
        "completion_rate": round(stats.completion_rate, 4),
        "rejected_sessions": result.rejected_sessions,
        "perceived_ttfa_p50_ms": round(perceived.percentiles[50], 1) if perceived else None,
        "perceived_ttfa_p95_ms": round(perceived.percentiles[95], 1) if perceived else None,
        "realtime_factor_p50": (round(stats.realtime_factor.percentiles[50], 3) if stats.realtime_factor else None),
    }


def _write_sweep_summary(out: Path, summary: list[dict[str, Any]]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "sweep.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Concurrency sweep",
        "",
        "| Sessions | Turns | Completed | Refused | Perceived TTFA p50 | p95 | Realtime factor p50 |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary:
        lines.append(
            "| {c} | {t} | {done:.0%} | {rej} | {p50} | {p95} | {rtf} |".format(
                c=row["concurrency"],
                t=row["turns"],
                done=row["completion_rate"],
                rej=row["rejected_sessions"],
                p50=f"{row['perceived_ttfa_p50_ms']:.0f} ms" if row["perceived_ttfa_p50_ms"] else "—",
                p95=f"{row['perceived_ttfa_p95_ms']:.0f} ms" if row["perceived_ttfa_p95_ms"] else "—",
                rtf=f"{row['realtime_factor_p50']:.2f}x" if row["realtime_factor_p50"] else "—",
            )
        )
    (out / "sweep.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def _cmd_report(args: argparse.Namespace) -> int:
    from deadair.report.render import render
    from deadair.report.saved import load_result

    result = load_result(args.run)
    out = args.out or args.run.parent
    report = render(result)
    markdown_path, html_path = report.write(out, stem=args.run.stem + "-report")
    print(f"Wrote {markdown_path}\n      {html_path}")
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    from deadair.report.gate import BudgetSpecError, parse_budgets
    from deadair.report.saved import load_result

    try:
        budgets = parse_budgets(args.budget)
    except BudgetSpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return _check_budgets(load_result(args.run), budgets)


def _cmd_compare(args: argparse.Namespace) -> int:
    from deadair.report.compare import compare, render_comparison
    from deadair.report.saved import load_result

    comparison = compare(
        load_result(args.base), load_result(args.candidate), threshold=args.threshold, floor_ms=args.floor_ms
    )
    markdown = render_comparison(comparison)
    print(markdown)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(markdown, encoding="utf-8")
    return 2 if args.fail_on_regression and comparison.regressions else 0


def _cmd_mock(args: argparse.Namespace) -> int:
    from deadair.mock import MockConfig, serve_mock

    config = MockConfig(
        host=args.host,
        port=args.port,
        vad_silence_ms=args.vad_silence_ms,
        stt_ms=args.stt_ms,
        llm_ms=args.llm_ms,
        tts_ms=args.tts_ms,
        max_sessions=args.max_sessions,
        fail_every=args.fail_every,
        stale_audio_after_cancel=args.stale_audio_after_cancel,
    )
    try:
        asyncio.run(serve_mock(config))
    except KeyboardInterrupt:
        return 130
    return 0


_COMMANDS = {
    "tap": _cmd_tap,
    "exporter": _cmd_exporter,
    "probe": _cmd_probe,
    "sweep": _cmd_sweep,
    "report": _cmd_report,
    "gate": _cmd_gate,
    "compare": _cmd_compare,
    "mock": _cmd_mock,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    _configure_logging(args.log_level)
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
