"""Tests for the Realtime event reducer.

The reducer is the only place a latency number is defined, so these tests are
about definitions as much as about code: what counts as the start of a turn, what
happens when the server speculates and changes its mind, and which clock a
measurement is taken against.
"""

from __future__ import annotations

import pytest

from localvoice.timeline import RecordedEvent, TimelineBuilder, replay


def event(t: float, type_: str, **payload) -> RecordedEvent:
    return RecordedEvent(t=t, wall=1_700_000_000.0 + t, type=type_, payload={"type": type_, **payload})


def simple_turn(builder: TimelineBuilder, base: float = 0.0, response_id: str = "resp_1") -> None:
    """One clean turn with every stage boundary present."""

    builder.mark_client_speech_start(base + 0.00, prompt="What is the capital of France?")
    builder.add(event(base + 0.02, "input_audio_buffer.speech_started"))
    builder.mark_client_speech_end(base + 1.50)
    builder.add(event(base + 1.90, "input_audio_buffer.speech_stopped"))
    builder.add(
        event(
            base + 2.05,
            "conversation.item.input_audio_transcription.completed",
            transcript="What is the capital of France?",
        )
    )
    builder.add(event(base + 2.06, "response.created", response={"id": response_id}))
    builder.add(event(base + 2.20, "response.output_text.delta", response_id=response_id, delta="Paris"))
    builder.add(event(base + 2.35, "response.output_audio.delta", response_id=response_id, delta="AAAA"))
    builder.add(event(base + 2.50, "response.output_audio.delta", response_id=response_id, delta="AAAA"))
    builder.add(event(base + 2.60, "response.output_audio.done", response_id=response_id))
    builder.add(event(base + 2.65, "response.done", response={"id": response_id, "status": "completed"}))


class TestStageBoundaries:
    def test_stage_latencies_are_measured_between_the_right_events(self) -> None:
        builder = TimelineBuilder("s1")
        simple_turn(builder)
        turn = builder.turns[0]

        assert turn.vad_eou_lag_ms == pytest.approx(400, abs=1)
        assert turn.asr_ms == pytest.approx(150, abs=1)
        assert turn.llm_ttft_ms == pytest.approx(150, abs=1)
        assert turn.tts_ttfb_ms == pytest.approx(150, abs=1)

    def test_server_and_user_clocks_differ_by_the_vad_lag(self) -> None:
        builder = TimelineBuilder("s1")
        simple_turn(builder)
        turn = builder.turns[0]

        # The server's headline number starts when its VAD noticed; the user's
        # starts when they actually stopped talking. The gap is the VAD lag, and
        # reporting only the first would hide 400 ms of real waiting.
        assert turn.ttfa_ms == pytest.approx(450, abs=1)
        assert turn.perceived_ttfa_ms == pytest.approx(850, abs=1)
        assert turn.perceived_ttfa_ms - turn.ttfa_ms == pytest.approx(turn.vad_eou_lag_ms, abs=1)

    def test_missing_stages_report_none_rather_than_zero(self) -> None:
        builder = TimelineBuilder("s1")
        builder.mark_client_speech_start(0.0)
        builder.mark_client_speech_end(1.0)
        builder.add(event(1.2, "input_audio_buffer.speech_stopped"))
        turn = builder.turns[0]

        # A turn that never transcribed has no ASR latency. Zero would be a lie
        # that averages into the percentiles as a suspiciously fast turn.
        assert turn.asr_ms is None
        assert turn.ttfa_ms is None
        assert not turn.completed

    def test_audio_delta_alone_counts_as_first_token(self) -> None:
        """A server that streams audio without text deltas still gets an LLM measure."""

        builder = TimelineBuilder("s1")
        builder.mark_client_speech_start(0.0)
        builder.mark_client_speech_end(1.0)
        builder.add(event(1.1, "input_audio_buffer.speech_stopped"))
        builder.add(event(1.2, "conversation.item.input_audio_transcription.completed", transcript="hi"))
        builder.add(event(1.25, "response.created", response={"id": "r"}))
        builder.add(event(1.40, "response.output_audio.delta", response_id="r", delta="AAAA"))
        turn = builder.turns[0]

        assert turn.first_token == turn.first_audio
        assert turn.tts_ttfb_ms == pytest.approx(0, abs=1)
        assert turn.llm_ttft_ms == pytest.approx(200, abs=1)


class TestTurnBinding:
    def test_two_turns_stay_separate(self) -> None:
        builder = TimelineBuilder("s1")
        simple_turn(builder, base=0.0, response_id="resp_1")
        simple_turn(builder, base=10.0, response_id="resp_2")

        assert len(builder.turns) == 2
        assert [turn.response_id for turn in builder.turns] == ["resp_1", "resp_2"]
        assert all(turn.completed for turn in builder.turns)

    def test_a_superseded_response_id_does_not_corrupt_the_first_turn(self) -> None:
        """The server may open a response, then settle on a different one.

        Upstream tracks speculative turns precisely because this happens. The
        reducer must not fold two response ids into one turn's timestamps, or a
        superseded response's audio would be charged to the turn that replaced it.
        """

        builder = TimelineBuilder("s1")
        builder.mark_client_speech_start(0.0)
        builder.mark_client_speech_end(1.0)
        builder.add(event(1.1, "input_audio_buffer.speech_stopped"))
        builder.add(event(1.2, "conversation.item.input_audio_transcription.completed", transcript="hi"))
        builder.add(event(1.25, "response.created", response={"id": "resp_a"}))
        builder.add(event(1.40, "response.output_audio.delta", response_id="resp_a", delta="AAAA"))
        builder.add(event(1.50, "response.done", response={"id": "resp_a", "status": "cancelled"}))
        builder.add(event(1.55, "response.created", response={"id": "resp_b"}))
        builder.add(event(1.70, "response.output_audio.delta", response_id="resp_b", delta="AAAA"))
        builder.add(event(1.80, "response.done", response={"id": "resp_b", "status": "completed"}))

        assert len(builder.turns) == 2
        assert builder.turns[0].response_id == "resp_a"
        assert builder.turns[0].status == "cancelled"
        assert builder.turns[1].response_id == "resp_b"
        assert builder.turns[1].status == "completed"

    def test_events_for_a_known_response_return_to_its_own_turn(self) -> None:
        """Interleaved responses must not have their deltas stolen by the newest turn."""

        builder = TimelineBuilder("s1")
        builder.mark_client_speech_start(0.0)
        builder.add(event(1.0, "response.created", response={"id": "resp_a"}))
        builder.add(event(1.1, "response.output_audio.delta", response_id="resp_a", delta="AAAA"))
        builder.add(event(1.2, "response.done", response={"id": "resp_a", "status": "completed"}))
        builder.mark_client_speech_start(2.0)
        builder.add(event(2.5, "response.created", response={"id": "resp_b"}))
        # A late delta from the first response arrives after the second opened.
        builder.add(event(2.6, "response.output_audio.delta", response_id="resp_a", delta="AAAAAAAA"))

        assert builder.turns[0].audio_bytes == 3 + 6
        assert builder.turns[1].audio_bytes == 0


class TestBargeIn:
    def test_barge_in_is_measured_to_the_last_audio_the_user_hears(self) -> None:
        builder = TimelineBuilder("s1")
        builder.mark_client_speech_start(0.0)
        builder.add(event(1.0, "response.created", response={"id": "r"}))
        builder.add(event(1.1, "response.output_audio.delta", response_id="r", delta="AAAA"))
        builder.mark_barge_in(2.0)
        builder.add(event(2.15, "response.output_audio.delta", response_id="r", delta="AAAA"))
        builder.add(event(2.40, "response.done", response={"id": "r", "status": "cancelled"}))

        # Charged to the trailing audio frame, not to response.done: audio that
        # arrives after the interruption is audio played over the user's voice.
        assert builder.turns[0].barge_in_ms == pytest.approx(150, abs=1)

    def test_a_clean_cancel_with_no_trailing_audio_still_reports(self) -> None:
        builder = TimelineBuilder("s1")
        builder.mark_client_speech_start(0.0)
        builder.add(event(1.0, "response.created", response={"id": "r"}))
        builder.add(event(1.1, "response.output_audio.delta", response_id="r", delta="AAAA"))
        builder.mark_barge_in(2.0)
        builder.add(event(2.20, "response.done", response={"id": "r", "status": "cancelled"}))

        assert builder.turns[0].barge_in_ms == pytest.approx(200, abs=1)


class TestAudioAccounting:
    @pytest.mark.parametrize(
        ("encoded", "expected"),
        [("", 0), ("AAAA", 3), ("AAA=", 2), ("AA==", 1), ("AAAAAAAA", 6)],
    )
    def test_base64_length_is_computed_without_decoding(self, encoded: str, expected: int) -> None:
        import base64

        from localvoice.timeline import _decoded_len

        assert _decoded_len(encoded) == expected
        if encoded:
            assert len(base64.b64decode(encoded)) == expected

    def test_realtime_factor_compares_audio_produced_against_time_taken(self) -> None:
        builder = TimelineBuilder("s1")
        builder.mark_client_speech_start(0.0)
        builder.add(event(1.0, "response.created", response={"id": "r"}))
        # 24000 bytes of 16-bit 24 kHz mono is 500 ms of speech.
        payload = "A" * 32_000  # decodes to 24000 bytes
        builder.add(event(1.0, "response.output_audio.delta", response_id="r", delta=payload))
        builder.add(event(1.25, "response.output_audio.done", response_id="r"))
        turn = builder.turns[0]

        assert turn.audio_duration_ms == pytest.approx(500, abs=1)
        # 500 ms of audio produced in 250 ms of wall time.
        assert turn.realtime_factor == pytest.approx(2.0, abs=0.01)


class TestErrors:
    def test_errors_are_recorded_against_the_turn(self) -> None:
        builder = TimelineBuilder("s1")
        builder.mark_client_speech_start(0.0)
        builder.add(event(1.0, "error", error={"type": "server_error", "message": "boom"}))

        assert builder.turns[0].errors == ["server_error"]

    def test_failed_response_captures_the_message(self) -> None:
        builder = TimelineBuilder("s1")
        builder.mark_client_speech_start(0.0)
        builder.add(event(1.0, "response.created", response={"id": "r"}))
        builder.add(
            event(
                1.5,
                "response.done",
                response={
                    "id": "r",
                    "status": "failed",
                    "status_details": {"error": {"message": "upstream_unavailable"}},
                },
            )
        )
        turn = builder.turns[0]

        assert turn.status == "failed"
        assert turn.errors == ["upstream_unavailable"]
        assert not turn.completed


class TestReplay:
    def test_replay_of_a_trace_reproduces_the_live_turns(self) -> None:
        """A published number must be recomputable from the trace behind it."""

        live = TimelineBuilder("s1")
        events = [
            event(0.1, "input_audio_buffer.speech_started"),
            event(1.9, "input_audio_buffer.speech_stopped"),
            event(2.05, "conversation.item.input_audio_transcription.completed", transcript="hi"),
            event(2.06, "response.created", response={"id": "r"}),
            event(2.20, "response.output_text.delta", response_id="r", delta="hello"),
            event(2.35, "response.output_audio.delta", response_id="r", delta="AAAA"),
            event(2.65, "response.done", response={"id": "r", "status": "completed"}),
        ]
        live.extend(events)
        replayed = replay(events, session_id="s1")

        assert len(replayed) == len(live.turns)
        assert replayed[0].ttfa_ms == pytest.approx(live.turns[0].ttfa_ms)
        assert replayed[0].to_json() == live.turns[0].to_json()
