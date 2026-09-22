"""Tests for prompt handling and the exporter.

The prompt tests are mostly about avoiding measurement bugs that would look like
system behaviour: mis-sized frames change the pacing the server's VAD sees, and a
prompt rotation that gives every concurrent session the same sentence would hide
cross-session leaks instead of exposing them.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from deadair.exporter import PipelineExporter
from deadair.probe.audio import (
    BYTES_PER_SAMPLE,
    PIPELINE_RATE_HZ,
    Prompt,
    PromptLibrary,
    chunk_pcm,
    read_wav,
    resample,
    silence,
    to_pcm16,
    write_wav,
)


def make_prompt_dir(path: Path, count: int = 4, rate: int = PIPELINE_RATE_HZ) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    entries = []
    for index in range(count):
        tone = 0.3 * np.sin(2 * np.pi * 220 * np.arange(rate) / rate).astype(np.float32)
        name = f"p{index}"
        write_wav(path / f"{name}.wav", tone, rate)
        entries.append({"name": name, "file": f"{name}.wav", "text": f"prompt number {index}"})
    (path / "manifest.json").write_text(json.dumps({"rate": rate, "prompts": entries}), encoding="utf-8")
    return path


class TestWavRoundTrip:
    def test_written_audio_reads_back_unchanged(self, tmp_path: Path) -> None:
        samples = np.linspace(-0.9, 0.9, 1000, dtype=np.float32)
        write_wav(tmp_path / "a.wav", samples, PIPELINE_RATE_HZ)
        restored, rate = read_wav(tmp_path / "a.wav")

        assert rate == PIPELINE_RATE_HZ
        np.testing.assert_allclose(restored, samples, atol=1e-4)

    def test_stereo_is_downmixed_to_mono(self, tmp_path: Path) -> None:
        left = np.full(100, 0.5, dtype=np.float32)
        right = np.full(100, -0.1, dtype=np.float32)
        interleaved = np.empty(200, dtype="<i2")
        interleaved[0::2] = (left * 32767).astype("<i2")
        interleaved[1::2] = (right * 32767).astype("<i2")
        with wave.open(str(tmp_path / "s.wav"), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(PIPELINE_RATE_HZ)
            handle.writeframes(interleaved.tobytes())

        samples, _ = read_wav(tmp_path / "s.wav")
        assert len(samples) == 100
        np.testing.assert_allclose(samples, 0.2, atol=1e-3)

    def test_non_16_bit_audio_is_rejected_with_a_clear_message(self, tmp_path: Path) -> None:
        with wave.open(str(tmp_path / "b.wav"), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(1)
            handle.setframerate(PIPELINE_RATE_HZ)
            handle.writeframes(b"\x00" * 100)

        with pytest.raises(ValueError, match="16-bit"):
            read_wav(tmp_path / "b.wav")


class TestFraming:
    def test_frames_are_the_requested_duration(self) -> None:
        pcm = silence(1.0, PIPELINE_RATE_HZ)
        frames = chunk_pcm(pcm, 20, PIPELINE_RATE_HZ)

        expected = PIPELINE_RATE_HZ * BYTES_PER_SAMPLE * 20 // 1000
        assert len(frames) == 50
        assert all(len(frame) == expected for frame in frames)

    def test_frames_never_split_a_sample_in_half(self) -> None:
        # An odd byte count per frame would shift every later sample by one byte
        # and turn the prompt into noise.
        for chunk_ms in (7, 13, 20, 33):
            frames = chunk_pcm(silence(0.5, PIPELINE_RATE_HZ), chunk_ms, PIPELINE_RATE_HZ)
            assert all(len(frame) % BYTES_PER_SAMPLE == 0 for frame in frames)

    def test_reassembled_frames_equal_the_original(self) -> None:
        pcm = to_pcm16(np.random.default_rng(0).uniform(-1, 1, 5000).astype(np.float32))
        assert b"".join(chunk_pcm(pcm, 20, PIPELINE_RATE_HZ)) == pcm

    def test_silence_is_the_requested_duration(self) -> None:
        assert len(silence(0.8, PIPELINE_RATE_HZ)) == int(0.8 * PIPELINE_RATE_HZ) * BYTES_PER_SAMPLE


class TestResample:
    def test_same_rate_is_a_no_op(self) -> None:
        samples = np.linspace(-1, 1, 100, dtype=np.float32)
        assert resample(samples, 16_000, 16_000) is samples

    def test_length_scales_with_the_rate_ratio(self) -> None:
        samples = np.zeros(16_000, dtype=np.float32)
        assert len(resample(samples, 16_000, 24_000)) == pytest.approx(24_000, rel=0.01)


class TestPromptLibrary:
    def test_prompts_cycle_when_more_turns_are_requested_than_exist(self, tmp_path: Path) -> None:
        library = PromptLibrary.load(make_prompt_dir(tmp_path / "p", count=3))
        assert library.get(0).name == library.get(3).name
        assert library.get(1).name == library.get(4).name

    def test_concurrent_sessions_say_different_things_at_the_same_turn(self, tmp_path: Path) -> None:
        """Otherwise a cross-session audio leak is invisible in the transcripts."""

        library = PromptLibrary.load(make_prompt_dir(tmp_path / "p", count=5))
        for turn in range(5):
            names = {library.for_session(session, turn).name for session in range(4)}
            assert len(names) == 4

    def test_a_missing_manifest_says_how_to_create_one(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="make_prompts.py"):
            PromptLibrary.load(tmp_path / "absent")

    def test_an_empty_library_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            PromptLibrary([])

    def test_prompt_duration_and_checksum(self) -> None:
        prompt = Prompt(name="p", text="hello", pcm=silence(0.5, PIPELINE_RATE_HZ), rate=PIPELINE_RATE_HZ)
        assert prompt.duration_s == pytest.approx(0.5)
        assert len(prompt.checksum) == 16


class TestExporter:
    def test_pool_states_are_published_including_the_empty_ones(self) -> None:
        """A state that empties out must read 0, not stop updating."""

        exporter = PipelineExporter("http://example")
        exporter._apply_pool(
            {
                "size": 4,
                "in_use": 2,
                "units": [
                    {"index": 0, "state": "active"},
                    {"index": 1, "state": "active"},
                    {"index": 2, "state": "idle"},
                    {"index": 3, "state": "idle"},
                ],
            }
        )
        rendered = exporter.registry.get_sample_value

        assert rendered("s2s_pool_size") == 4
        assert rendered("s2s_pool_in_use") == 2
        assert rendered("s2s_pool_units", {"state": "active"}) == 2
        assert rendered("s2s_pool_units", {"state": "idle"}) == 2
        assert rendered("s2s_pool_units", {"state": "stuck"}) == 0
        assert rendered("s2s_pool_units", {"state": "draining"}) == 0

    def test_stuck_units_are_counted(self) -> None:
        exporter = PipelineExporter("http://example")
        exporter._apply_pool({"size": 2, "in_use": 1, "units": [{"state": "stuck"}, {"state": "idle"}]})
        assert exporter.registry.get_sample_value("s2s_pool_units", {"state": "stuck"}) == 1

    def test_usage_counters_and_error_types_are_published(self) -> None:
        exporter = PipelineExporter("http://example")
        exporter._apply_usage(
            {
                "connections": 7,
                "turns": 21,
                "responses_completed": 19,
                "responses_cancelled": 2,
                "audio_duration_s": 48.5,
                "errors_by_type": {"response_failed": 3},
            }
        )
        rendered = exporter.registry.get_sample_value

        assert rendered("s2s_turns_reported") == 21
        assert rendered("s2s_responses_cancelled_reported") == 2
        assert rendered("s2s_output_audio_seconds_reported") == pytest.approx(48.5)
        assert rendered("s2s_errors_reported", {"type": "response_failed"}) == 3

    async def test_scrape_body_matches_its_declared_content_type(self) -> None:
        """Same trap as the tap's /metrics; see that test for why it matters."""

        import httpx

        exporter = PipelineExporter("http://example")
        transport = httpx.ASGITransport(app=exporter.build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as client:
            response = await client.get("/metrics")

        content_type = response.headers["content-type"]
        if "openmetrics" in content_type:
            assert response.text.endswith("# EOF\n")
        else:
            assert "text/plain" in content_type
            assert not response.text.endswith("# EOF\n")

    def test_malformed_payloads_are_ignored_rather_than_raising(self) -> None:
        exporter = PipelineExporter("http://example")
        exporter._apply_usage({"turns": "not a number", "errors_by_type": "not a dict"})
        exporter._apply_pool({"size": None, "units": ["not a dict", {"state": "unknown"}]})

        assert exporter.registry.get_sample_value("s2s_turns_reported") == 0
