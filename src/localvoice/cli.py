"""Command line entry point.

::

    localvoice tap       run the measuring, fault-injecting LLM proxy
    localvoice exporter  republish the pipeline's own counters as Prometheus metrics
    localvoice probe     drive Realtime sessions and record the latency budget
    localvoice sweep     run a concurrency sweep and report where it breaks
    localvoice report    re-render a saved run without re-running it
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

from localvoice import __version__

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
        prog="localvoice",
        description="Measure a local OpenAI-Realtime voice deployment.",
    )
    parser.add_argument("--version", action="version", version=f"localvoice {__version__}")
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

    sweep = sub.add_parser("sweep", help="Run the same scenario at increasing concurrency.")
    _add_probe_arguments(sweep)
    sweep.add_argument("--levels", default="1,2,4,8", help="Comma-separated concurrency levels. Default is 1,2,4,8.")

    report = sub.add_parser("report", help="Re-render a saved run.")
    report.add_argument("run", type=Path, help="Path to a run JSON produced by probe or sweep.")
    report.add_argument("--out", type=Path, default=None, help="Output directory. Defaults beside the input.")

    return parser


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
    from localvoice.probe.client import SessionConfig

    config = SessionConfig(url=args.url, send_rate=args.send_rate)
    if args.instructions:
        config.instructions = args.instructions
    if args.voice:
        config.voice = args.voice
    return config


def _load_prompts(args: argparse.Namespace) -> Any:
    from localvoice.probe.audio import PromptLibrary

    return PromptLibrary.load(args.prompts, target_rate=args.send_rate)


def _run_and_report(args: argparse.Namespace, spec: Any, stem: str) -> Any:
    from localvoice.probe import runner
    from localvoice.report.render import render

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

    from localvoice.tap.faults import FaultSpecError, parse_specs
    from localvoice.tap.proxy import TapConfig, build_app

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

    from localvoice.exporter import build_app

    app = build_app(args.pipeline_url, interval_s=args.interval)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    from localvoice.probe.runner import RunSpec

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
    _run_and_report(args, spec, stem=f"probe-c{args.concurrency}")
    return 0


def _cmd_sweep(args: argparse.Namespace) -> int:
    from localvoice.probe.runner import RunSpec

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
    from localvoice.report.stats import summarize

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
    from localvoice.probe.client import SessionResult
    from localvoice.probe.runner import RunResult, RunSpec
    from localvoice.report.render import render
    from localvoice.timeline import TurnTimeline

    payload = json.loads(args.run.read_text(encoding="utf-8"))
    spec_payload = payload.get("spec", {})
    spec = RunSpec(
        name=spec_payload.get("name", args.run.stem),
        concurrency=int(spec_payload.get("concurrency", 1)),
        turns=int(spec_payload.get("turns", 0)),
        warmup_turns=int(spec_payload.get("warmup_turns", 0)),
        interval_s=float(spec_payload.get("interval_s", 0.0)),
        barge_in_after_s=spec_payload.get("barge_in_after_s"),
        faults=list(spec_payload.get("faults") or []),
        notes=spec_payload.get("notes", ""),
    )
    sessions = [_session_from_json(item) for item in payload.get("sessions", [])]
    result = RunResult(
        spec=spec,
        started_at=payload.get("started_at", ""),
        duration_s=float(payload.get("duration_s", 0.0)),
        sessions=sessions,
        environment=payload.get("environment", {}),
    )
    # A saved run already had its warmup trimmed on the way out, so re-rendering
    # must not trim it a second time.
    result.spec.warmup_turns = 0
    _ = TurnTimeline, SessionResult  # keep the imports honest for type checkers

    out = args.out or args.run.parent
    report = render(result)
    markdown_path, html_path = report.write(out, stem=args.run.stem + "-report")
    print(f"Wrote {markdown_path}\n      {html_path}")
    return 0


def _session_from_json(item: dict[str, Any]) -> Any:
    from localvoice.probe.client import SessionResult
    from localvoice.timeline import TurnTimeline

    turns: list[TurnTimeline] = []
    for raw in item.get("turns", []):
        turn = TurnTimeline(
            session_id=raw.get("session_id", ""),
            turn_index=int(raw.get("turn_index", 0)),
            response_id=raw.get("response_id"),
            status=raw.get("status"),
            prompt=raw.get("prompt"),
            transcript=raw.get("transcript"),
            response_text=raw.get("response_text", ""),
            audio_bytes=int(raw.get("audio_bytes", 0)),
            errors=list(raw.get("errors") or []),
        )
        # Saved runs carry derived latencies, not the timestamps behind them.
        # Reconstruct a consistent set of monotonic marks from those durations so
        # the renderer sees the same numbers it saw originally.
        _restore_marks(turn, raw.get("latency_ms") or {}, raw.get("realtime_factor"))
        turns.append(turn)

    result = SessionResult(
        session_index=int(item.get("session_index", 0)),
        session_id=item.get("session_id", ""),
        turns=turns,
    )
    result.connect_ms = item.get("connect_ms")
    result.rejected = item.get("rejected")
    result.error = item.get("error")
    return result


def _restore_marks(turn: Any, latency: dict[str, Any], realtime_factor: Any) -> None:
    """Rebuild monotonic marks from saved durations, anchored at zero."""

    def value(name: str) -> float | None:
        raw = latency.get(name)
        return float(raw) / 1000.0 if isinstance(raw, (int, float)) else None

    turn.client_speech_end = 0.0
    lag = value("vad_eou_lag")
    turn.speech_stopped = lag if lag is not None else 0.0
    asr = value("asr")
    if asr is not None:
        turn.transcript_done = turn.speech_stopped + asr
    ttft = value("llm_ttft")
    if ttft is not None and turn.transcript_done is not None:
        turn.first_token = turn.transcript_done + ttft
        turn.response_created = turn.transcript_done
    ttfb = value("tts_ttfb")
    if ttfb is not None and turn.first_token is not None:
        turn.first_audio = turn.first_token + ttfb
    total = value("response_total")
    if total is not None and turn.response_created is not None:
        turn.response_done = turn.response_created + total
    if isinstance(realtime_factor, (int, float)) and turn.first_audio is not None and realtime_factor > 0:
        turn.audio_done = turn.first_audio + turn.audio_duration_ms / 1000.0 / float(realtime_factor)


_COMMANDS = {
    "tap": _cmd_tap,
    "exporter": _cmd_exporter,
    "probe": _cmd_probe,
    "sweep": _cmd_sweep,
    "report": _cmd_report,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    _configure_logging(args.log_level)
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
