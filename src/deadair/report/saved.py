"""Load a run JSON written by ``probe`` or ``sweep`` back into a :class:`RunResult`.

``report``, ``gate`` and ``compare`` all work from saved runs, so a number can be
re-rendered, checked against a budget, or compared with a later run without
driving the stack again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from deadair.probe.client import SessionResult
from deadair.probe.runner import RunResult, RunSpec
from deadair.timeline import TurnTimeline


def load_result(path: Path) -> RunResult:
    payload = json.loads(path.read_text(encoding="utf-8"))
    spec_payload = payload.get("spec", {})
    spec = RunSpec(
        name=spec_payload.get("name", path.stem),
        concurrency=int(spec_payload.get("concurrency", 1)),
        turns=int(spec_payload.get("turns", 0)),
        warmup_turns=int(spec_payload.get("warmup_turns", 0)),
        interval_s=float(spec_payload.get("interval_s", 0.0)),
        barge_in_after_s=spec_payload.get("barge_in_after_s"),
        faults=list(spec_payload.get("faults") or []),
        notes=spec_payload.get("notes", ""),
    )
    # The saved run keeps every turn, warmup included, and records how many were
    # warmup. Re-rendering must trim the same count the original run trimmed, or a
    # report regenerated from a trace quietly disagrees with the one it replaces.
    sessions = [_session_from_json(item) for item in payload.get("sessions", [])]
    return RunResult(
        spec=spec,
        started_at=payload.get("started_at", ""),
        duration_s=float(payload.get("duration_s", 0.0)),
        sessions=sessions,
        environment=payload.get("environment", {}),
    )


def _session_from_json(item: dict[str, Any]) -> SessionResult:
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
            protocol_violations=list(raw.get("protocol_violations") or []),
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
    # Barge-in has no timestamp of its own in the saved run, only a duration.
    # Anchor it anywhere consistent; the report only ever reads the difference.
    barge_in = value("barge_in")
    if barge_in is not None and turn.first_audio is not None:
        turn.barge_in_sent = turn.first_audio
        turn.last_audio_after_barge_in = turn.first_audio + barge_in
    if isinstance(realtime_factor, (int, float)) and turn.first_audio is not None and realtime_factor > 0:
        turn.audio_done = turn.first_audio + turn.audio_duration_ms / 1000.0 / float(realtime_factor)
