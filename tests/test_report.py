"""Tests for the statistics and the rendered report.

Statistics tests exist mostly to pin down the choices that would otherwise drift:
nearest-rank rather than interpolated percentiles, stage medians that are allowed
not to sum, and a budget built from one real turn so that it does.
"""

from __future__ import annotations

import json

import pytest

from deadair.probe.client import SessionResult
from deadair.probe.runner import RunResult, RunSpec
from deadair.report.render import render, render_markdown
from deadair.report.stats import budget, describe, percentile, representative_turn, summarize
from deadair.timeline import TurnTimeline


def make_turn(
    index: int = 0,
    *,
    vad: float = 0.15,
    asr: float = 0.02,
    llm: float = 0.20,
    tts: float = 0.10,
    status: str = "completed",
    audio_bytes: int = 48_000,
) -> TurnTimeline:
    """A turn with the given stage durations, in seconds."""

    turn = TurnTimeline(session_id="s", turn_index=index, response_id=f"r{index}", status=status)
    turn.client_speech_end = 0.0
    turn.speech_stopped = vad
    turn.transcript_done = turn.speech_stopped + asr
    turn.response_created = turn.transcript_done
    turn.first_token = turn.transcript_done + llm
    turn.first_audio = turn.first_token + tts
    turn.audio_done = turn.first_audio + 0.5
    turn.response_done = turn.audio_done
    turn.audio_bytes = audio_bytes
    return turn


class TestPercentile:
    def test_nearest_rank_returns_an_observed_value(self) -> None:
        sample = [10.0, 20.0, 30.0, 40.0]
        for q in (1, 25, 50, 75, 99):
            assert percentile(sample, q) in sample

    @pytest.mark.parametrize(
        ("q", "expected"),
        [(0, 10.0), (25, 10.0), (50, 20.0), (75, 30.0), (100, 40.0)],
    )
    def test_known_ranks(self, q: float, expected: float) -> None:
        assert percentile([40.0, 10.0, 30.0, 20.0], q) == expected

    def test_single_sample_is_every_percentile(self) -> None:
        assert percentile([7.0], 50) == 7.0
        assert percentile([7.0], 99) == 7.0

    def test_empty_sample_is_an_error_not_a_zero(self) -> None:
        with pytest.raises(ValueError):
            percentile([], 50)


class TestDescribe:
    def test_none_values_are_excluded_rather_than_counted_as_zero(self) -> None:
        distribution = describe("x", "X", [100.0, None, 200.0, None])  # type: ignore[list-item]
        assert distribution is not None
        assert distribution.count == 2
        assert distribution.mean == 150.0

    def test_a_sample_with_nothing_measured_returns_none(self) -> None:
        assert describe("x", "X", [None, None]) is None  # type: ignore[list-item]

    def test_infinities_are_dropped(self) -> None:
        distribution = describe("x", "X", [1.0, float("inf"), float("nan"), 3.0])
        assert distribution is not None
        assert distribution.count == 2


class TestSummarize:
    def test_counts_split_by_outcome(self) -> None:
        turns = [
            make_turn(0),
            make_turn(1),
            make_turn(2, status="failed"),
            make_turn(3, status="cancelled"),
        ]
        turns[2].errors.append("upstream_unavailable")
        stats = summarize(turns)

        assert stats.total_turns == 4
        assert stats.completed_turns == 2
        assert stats.failed_turns == 1
        assert stats.cancelled_turns == 1
        assert stats.completion_rate == 0.5
        assert stats.error_counts == {"upstream_unavailable": 1}

    def test_stage_medians_need_not_sum_to_the_headline_median(self) -> None:
        """The slowest ASR turn and the slowest TTS turn are different turns.

        Forcing the parts to reconcile would hide that, so the report states the
        discrepancy instead of normalising it away. This test locks that in.
        """

        turns = [make_turn(0, asr=0.50, tts=0.02), make_turn(1, asr=0.02, tts=0.50)]
        stats = summarize(turns)
        stage_sum = sum(d.p50 for d in stats.stages)
        perceived = stats.stage("perceived_ttfa")

        assert perceived is not None
        assert stage_sum != pytest.approx(perceived.p50, abs=1.0)

    def test_realtime_factor_is_summarized_in_multiples_not_milliseconds(self) -> None:
        stats = summarize([make_turn(0)])
        assert stats.realtime_factor is not None
        assert stats.realtime_factor.unit == "x"


class TestBudget:
    def test_slices_sum_to_the_turn_total_and_shares_to_one(self) -> None:
        turn = make_turn(0, vad=0.15, asr=0.02, llm=0.20, tts=0.10)
        slices = budget(turn)

        assert [s.name for s in slices] == ["vad_eou_lag", "asr", "llm_ttft", "tts_ttfb"]
        assert sum(s.ms for s in slices) == pytest.approx(470, abs=1)
        assert sum(s.share for s in slices) == pytest.approx(1.0, abs=1e-9)

    def test_a_turn_with_no_stages_yields_no_slices(self) -> None:
        assert budget(TurnTimeline(session_id="s", turn_index=0)) == []

    def test_representative_turn_is_a_real_turn_near_the_median(self) -> None:
        turns = [make_turn(i, llm=0.1 * (i + 1)) for i in range(5)]
        chosen = representative_turn(turns)

        assert chosen is not None
        assert chosen in turns
        assert chosen.completed

    def test_representative_turn_ignores_incomplete_turns(self) -> None:
        good = make_turn(0)
        bad = TurnTimeline(session_id="s", turn_index=1, status="failed")
        assert representative_turn([bad, good]) is good

    def test_no_completed_turn_yields_no_representative(self) -> None:
        assert representative_turn([TurnTimeline(session_id="s", turn_index=0)]) is None


def make_result(turns: list[TurnTimeline], warmup: int = 0) -> RunResult:
    session = SessionResult(session_index=0, session_id="s", turns=turns)
    return RunResult(
        spec=RunSpec(name="test run", concurrency=1, turns=len(turns), warmup_turns=warmup),
        started_at="2026-09-22T00:00:00+00:00",
        duration_s=12.5,
        sessions=[session],
        environment={"host": "testhost", "gpus": ["NVIDIA GeForce RTX 5090, 32607 MiB, 580.173.02"]},
    )


class TestRender:
    def test_markdown_reports_the_headline_and_the_caveat(self) -> None:
        result = make_result([make_turn(i) for i in range(4)])
        report = render(result)

        assert "# test run" in report.markdown
        assert "Time to first audio (user clock)" in report.markdown
        # The caveat about non-summing percentiles must survive refactors.
        assert "rarely the same turn" in report.markdown
        assert "RTX 5090" in report.markdown

    def test_warmup_turns_are_excluded_from_the_statistics(self) -> None:
        slow_warmup = make_turn(0, llm=5.0)
        steady = [make_turn(i, llm=0.2) for i in range(1, 5)]
        result = make_result([slow_warmup, *steady], warmup=1)

        assert len(result.measured_turns) == 4
        assert result.warmup_turns == [slow_warmup]
        stats = summarize(result.measured_turns)
        llm = stats.stage("llm_ttft")
        assert llm is not None
        # The 5 s warmup must not drag the steady-state median.
        assert llm.percentiles[50] == pytest.approx(200, abs=1)

    def test_html_is_self_contained_with_no_remote_loads(self) -> None:
        """The report must open offline, so nothing may be fetched at view time.

        Checks for things that actually load a resource. The bare string "http"
        would be a false positive: the SVG namespace is a URI, not a request.
        """

        import re

        report = render(make_result([make_turn(i) for i in range(3)]))
        assert report.html.startswith("<!DOCTYPE html>")

        loaders = re.findall(
            r"""(?:src|href)\s*=\s*["']https?://|@import|\bfetch\s*\(|\bimportScripts\s*\(""",
            report.html,
        )
        assert loaders == []

    def test_html_embeds_the_data_as_valid_json(self) -> None:
        report = render(make_result([make_turn(i) for i in range(3)]))
        start = report.html.index('<script id="payload" type="application/json">') + len(
            '<script id="payload" type="application/json">'
        )
        end = report.html.index("</script>", start)
        payload = json.loads(report.html[start:end])

        assert payload["spec"]["name"] == "test run"
        assert len(payload["turns"]) == 3
        assert len(payload["budget"]) == 4

    def test_a_run_where_every_turn_failed_still_renders(self) -> None:
        """An empty-sample report is the one you most need to read."""

        turns = [TurnTimeline(session_id="s", turn_index=i, status="failed") for i in range(3)]
        for turn in turns:
            turn.errors.append("upstream_unavailable")
        result = make_result(turns)

        markdown = render_markdown(result, summarize(turns), [])
        assert "Completed | 0 (0.0%)" in markdown
        assert "upstream_unavailable" in markdown

    def test_rerendering_a_saved_run_reproduces_the_same_statistics(self, tmp_path) -> None:
        """A report regenerated from a saved run must not disagree with the original.

        The saved run keeps every turn, warmup included, so re-rendering has to trim
        the same count. Getting this wrong changes n and every percentile with it,
        silently, which is worse than failing outright.
        """

        from deadair.cli import main
        from deadair.probe.runner import write_result

        slow_warmup = make_turn(0, llm=5.0)
        steady = [make_turn(i, llm=0.2) for i in range(1, 6)]
        original = make_result([slow_warmup, *steady], warmup=1)
        before = summarize(original.measured_turns)

        run_path = tmp_path / "run.json"
        write_result(run_path, original)
        assert main(["report", str(run_path), "--out", str(tmp_path)]) == 0

        rendered = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        llm = before.stage("llm_ttft")
        assert llm is not None and llm.count == 5
        # n and the median both survive the round trip.
        assert f"| {llm.count} |" in rendered
        assert "200 ms" in rendered
        assert "5.00 s" not in rendered

    def test_rerendering_keeps_the_barge_in_measure(self, tmp_path) -> None:
        """Barge-in is stored only as a duration, so it has to be rebuilt by hand.

        Without that, a regenerated barge-in report silently loses the one row the
        run existed to produce.
        """

        from deadair.cli import main
        from deadair.probe.runner import write_result

        turn = make_turn(0, status="cancelled")
        turn.barge_in_sent = turn.first_audio
        turn.last_audio_after_barge_in = (turn.first_audio or 0.0) + 0.393
        assert turn.barge_in_ms == pytest.approx(393, abs=1)

        run_path = tmp_path / "bargein.json"
        write_result(run_path, make_result([turn]))
        assert main(["report", str(run_path), "--out", str(tmp_path)]) == 0

        rendered = (tmp_path / "bargein-report.md").read_text(encoding="utf-8")
        assert "Barge-in to last audio" in rendered
        assert "393 ms" in rendered

    def test_report_writes_both_files(self, tmp_path) -> None:
        report = render(make_result([make_turn(0)]))
        markdown_path, html_path = report.write(tmp_path, stem="run")

        assert markdown_path.read_text(encoding="utf-8").startswith("# ")
        assert html_path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
