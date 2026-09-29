"""The probe, reducer, gate and comparison run end to end against the mock server.

Each negative drill checks a detector against a known answer: the mock is told to
refuse, fail, or misbehave, and the measurement must notice.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from deadair.mock import MockConfig, running
from deadair.probe import runner
from deadair.probe.audio import Prompt, PromptLibrary, to_pcm16
from deadair.probe.client import SessionConfig
from deadair.probe.runner import RunSpec
from deadair.report.compare import compare
from deadair.report.gate import evaluate, parse_budgets
from deadair.report.saved import load_result
from deadair.report.stats import summarize

RATE = 16_000
FAST = MockConfig(port=0, vad_silence_ms=100, stt_ms=10, llm_ms=20, tts_ms=10, audio_chunks=6, chunk_gap_ms=30)


def tone_prompts(count: int = 3) -> PromptLibrary:
    prompts = []
    for index in range(count):
        t = np.arange(int(0.3 * RATE)) / RATE
        samples = 0.3 * np.sin(2 * np.pi * (220 + 110 * index) * t)
        prompts.append(Prompt(name=f"tone{index}", text=f"tone {index}", pcm=to_pcm16(samples), rate=RATE))
    return PromptLibrary(prompts)


async def probe(config: MockConfig, spec: RunSpec) -> runner.RunResult:
    async with running(config) as url:
        session = SessionConfig(url=url, trailing_silence_s=0.3, turn_timeout_s=5.0)
        return await runner.run(spec, session, tone_prompts())


async def test_clean_run_completes_every_turn_and_passes_budgets():
    result = await probe(FAST, RunSpec(name="clean", concurrency=2, turns=3, warmup_turns=1))
    stats = summarize(result.measured_turns)

    assert stats.total_turns == 4
    assert stats.completion_rate == 1.0
    assert stats.protocol_violations == 0
    perceived = stats.stage("perceived_ttfa")
    assert perceived is not None
    # The mock's fixed delays set a floor: STT, LLM and TTS after speech_stopped,
    # plus the VAD silence window before it. The probe sends each 20 ms frame at
    # the start of its window, so the server finishes counting silence one frame
    # before the audio's own end.
    server_clock = stats.stage("ttfa")
    assert server_clock is not None and server_clock.p50 >= 10 + 20 + 10
    assert perceived.p50 >= 100 - 20 + 10 + 20 + 10

    budgets = parse_budgets(["completion_rate>=1", "protocol_violations<=0", "perceived_ttfa:p95<=2000"])
    assert all(outcome.passed for outcome in evaluate(result, budgets))


async def test_speaking_over_a_reply_cancels_it_and_measures_barge_in():
    result = await probe(FAST, RunSpec(name="barge", turns=2, warmup_turns=0, barge_in_after_s=0.02))
    stats = summarize(result.measured_turns)

    assert stats.cancelled_turns >= 1
    assert stats.stage("barge_in") is not None
    assert stats.protocol_violations == 0


async def test_audio_after_cancel_is_reported_as_a_violation():
    config = replace(FAST, stale_audio_after_cancel=True)
    result = await probe(config, RunSpec(name="stale", turns=2, warmup_turns=0, barge_in_after_s=0.02))
    stats = summarize(result.measured_turns)

    assert stats.violation_counts.get("audio_after_response_done", 0) >= 1
    (outcome,) = evaluate(result, parse_budgets(["protocol_violations<=0"]))
    assert not outcome.passed


async def test_sessions_past_the_limit_are_refused_not_slowed():
    result = await probe(replace(FAST, max_sessions=1), RunSpec(name="limit", concurrency=2, turns=2, warmup_turns=0))

    assert result.rejected_sessions == 1
    (outcome,) = evaluate(result, parse_budgets(["rejected_sessions<=0"]))
    assert not outcome.passed


async def test_failed_responses_lower_the_completion_rate():
    result = await probe(replace(FAST, fail_every=2), RunSpec(name="fail", turns=4, warmup_turns=0))
    stats = summarize(result.measured_turns)

    assert stats.failed_turns == 2
    assert stats.completion_rate == 0.5
    assert stats.error_counts.get("mock failure") == 2


async def test_compare_names_the_stage_that_slowed(tmp_path: Path):
    spec = RunSpec(name="llm", turns=4, warmup_turns=1)
    base = await probe(FAST, spec)
    candidate = await probe(replace(FAST, llm_ms=250), spec)

    # Round trip through the saved JSON, which is what the CLI compares.
    runner.write_result(tmp_path / "base.json", base)
    runner.write_result(tmp_path / "candidate.json", candidate)
    comparison = compare(load_result(tmp_path / "base.json"), load_result(tmp_path / "candidate.json"))

    assert "llm_ttft" in comparison.regressions
    assert "asr" not in comparison.regressions
    assert "tts_ttfb" not in comparison.regressions


@pytest.mark.parametrize(
    "text",
    ["perceived_ttfa:p42<=100", "nonsense<=1", "completion_rate:p95>=0.9", "perceived_ttfa<1200"],
)
def test_bad_budgets_are_rejected(text: str):
    from deadair.report.gate import BudgetSpecError, parse_budget

    with pytest.raises(BudgetSpecError):
        parse_budget(text)


def test_budget_defaults_to_p95():
    (budget,) = parse_budgets(["llm_ttft<=300"])
    assert budget.stat == "p95"
    assert budget.text == "llm_ttft:p95<=300"
