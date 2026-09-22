"""Run a scenario across N concurrent Realtime sessions and collect the results.

The runner owns three things a load tool has to get right:

**A warmup that is excluded from the statistics.** First-turn latency on this
stack is dominated by lazy model warmup and an empty KV cache. Reporting it mixed
in with steady-state numbers makes a deployment look far worse than it is; hiding
it entirely makes cold start look free. It is measured, labelled, and kept out of
the headline percentiles.

**Sessions that start together.** Concurrency claims mean nothing if sessions
trickle in, so sessions rendezvous on a barrier and begin speaking at once.

**Failure that is data, not a crash.** A session rejected for pool exhaustion is a
result — it is how you find the concurrency ceiling — so it is recorded and the
run continues.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import platform
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from localvoice.probe.audio import PromptLibrary
from localvoice.probe.client import RealtimeProbe, SessionConfig, SessionResult, write_trace
from localvoice.timeline import TurnTimeline

logger = logging.getLogger("localvoice.probe.runner")


@dataclass
class RunSpec:
    """One measurement scenario."""

    name: str = "latency"
    concurrency: int = 1
    turns: int = 6
    warmup_turns: int = 1
    interval_s: float = 0.0
    barge_in_after_s: float | None = None
    faults: list[str] = field(default_factory=list)
    tap_url: str | None = None
    notes: str = ""


@dataclass
class RunResult:
    spec: RunSpec
    started_at: str
    duration_s: float
    sessions: list[SessionResult]
    environment: dict[str, Any]

    @property
    def measured_turns(self) -> list[TurnTimeline]:
        """Turns that count toward the headline statistics."""

        return [turn for session in self.sessions for turn in session.turns[self.spec.warmup_turns :]]

    @property
    def warmup_turns(self) -> list[TurnTimeline]:
        return [turn for session in self.sessions for turn in session.turns[: self.spec.warmup_turns]]

    @property
    def rejected_sessions(self) -> int:
        return sum(1 for session in self.sessions if session.rejected)

    def to_json(self) -> dict[str, Any]:
        return {
            "spec": {
                "name": self.spec.name,
                "concurrency": self.spec.concurrency,
                "turns": self.spec.turns,
                "warmup_turns": self.spec.warmup_turns,
                "interval_s": self.spec.interval_s,
                "barge_in_after_s": self.spec.barge_in_after_s,
                "faults": self.spec.faults,
                "notes": self.spec.notes,
            },
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 2),
            "environment": self.environment,
            "rejected_sessions": self.rejected_sessions,
            "sessions": [session.to_json() for session in self.sessions],
        }


def collect_environment() -> dict[str, Any]:
    """Record enough about the host that a number can be argued with later."""

    environment: dict[str, Any] = {
        "host": platform.node(),
        "platform": platform.platform(),
        "python": platform.python_version(),
    }
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if output.returncode == 0:
            environment["gpus"] = [line.strip() for line in output.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        pass
    return environment


async def apply_faults(tap_url: str, faults: list[str]) -> None:
    """Reconfigure the tap's fault set in place, without restarting the stack."""

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(f"{tap_url.rstrip('/')}/faults", json={"faults": faults})
        response.raise_for_status()
        logger.info("tap faults set to %s", response.json().get("faults"))


async def _run_session(
    index: int,
    spec: RunSpec,
    session_config: SessionConfig,
    prompts: PromptLibrary,
    barrier: asyncio.Barrier,
) -> SessionResult:
    probe = RealtimeProbe(session_config, session_index=index)
    try:
        async with probe.connect():
            # Sessions that got a slot wait here for the rest, so the load starts
            # as a step rather than a ramp.
            try:
                await asyncio.wait_for(barrier.wait(), timeout=30.0)
            except (asyncio.TimeoutError, asyncio.BrokenBarrierError):
                logger.debug("session %d proceeding without a full barrier", index)

            if spec.barge_in_after_s is not None:
                return await _run_barge_in(probe, spec, prompts)
            return await probe.converse(prompts, turns=spec.turns, interval_s=spec.interval_s)
    except Exception as exc:  # noqa: BLE001 - a failed session is a datapoint
        probe.result.error = probe.result.error or f"{type(exc).__name__}: {exc}"
        probe.result.turns = probe.builder.turns
        logger.warning("session %d failed: %s", index, probe.result.error)
        # Release peers so one refused connection cannot deadlock the barrier.
        # asyncio.Barrier.abort is a coroutine; unawaited it silently does nothing.
        with contextlib.suppress(Exception):
            await barrier.abort()
        return probe.result


async def _run_barge_in(probe: RealtimeProbe, spec: RunSpec, prompts: PromptLibrary) -> SessionResult:
    """Alternate a normal turn with an interruption of the reply it triggers."""

    assert spec.barge_in_after_s is not None
    for turn_index in range(spec.turns):
        if probe.result.rejected:
            break
        prompt = prompts.for_session(probe.session_index, turn_index)
        if turn_index < spec.warmup_turns:
            await probe.speak(prompt)
            continue
        speaking = asyncio.create_task(probe.speak(prompt))
        interrupt = prompts.for_session(probe.session_index, turn_index + 1)
        await probe.barge_in(interrupt, after_s=spec.barge_in_after_s)
        await speaking
    probe.result.turns = probe.builder.turns
    return probe.result


async def run(
    spec: RunSpec,
    session_config: SessionConfig,
    prompts: PromptLibrary,
    trace_dir: Path | None = None,
) -> RunResult:
    if spec.faults:
        if not spec.tap_url:
            raise ValueError("faults were requested but no tap URL was given")
        await apply_faults(spec.tap_url, spec.faults)

    barrier = asyncio.Barrier(spec.concurrency)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()

    results = await asyncio.gather(
        *(_run_session(index, spec, session_config, prompts, barrier) for index in range(spec.concurrency))
    )
    duration = time.monotonic() - started

    if spec.faults and spec.tap_url:
        # Always hand the stack back healthy; a leftover fault would silently
        # poison whatever scenario runs next.
        await apply_faults(spec.tap_url, [])

    if trace_dir is not None:
        trace_dir.mkdir(parents=True, exist_ok=True)
        for result in results:
            write_trace(trace_dir / f"session_{result.session_index:03d}.jsonl", result)

    return RunResult(
        spec=spec,
        started_at=started_at,
        duration_s=duration,
        sessions=list(results),
        environment=collect_environment(),
    )


def write_result(path: Path, result: RunResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_json(), indent=2) + "\n", encoding="utf-8")
