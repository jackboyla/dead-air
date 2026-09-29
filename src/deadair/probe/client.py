"""A single Realtime session: speak, listen, time everything.

The client speaks the core OpenAI Realtime GA event set over WebSocket, so it
works against this deployment, against hosted OpenAI, or against anything else
that implements the protocol. That portability is the point — a latency number
only means something if you can measure the alternative the same way.

Audio is streamed in real time rather than dumped. A probe that pushes a whole
utterance in one frame measures how fast the server drains a buffer, not how fast
it answers a person. Server-side VAD needs the pacing to behave as it would with a
microphone, so frames go out on a wall-clock schedule with the trailing silence a
real speaker leaves behind.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets

from deadair.probe.audio import Prompt, PromptLibrary, chunk_pcm, silence
from deadair.timeline import RecordedEvent, TimelineBuilder, TurnTimeline

logger = logging.getLogger("deadair.probe")

FRAME_MS = 20


@dataclass
class SessionConfig:
    url: str = "ws://127.0.0.1:18765/v1/realtime"
    model: str = "local-gemma"
    api_key: str = "not-needed"
    send_rate: int = 16_000
    instructions: str = "You are a voice assistant. Answer in one short sentence. Never use lists or markdown."
    voice: str | None = None
    # Silence after each prompt so server-side VAD can declare the turn over.
    trailing_silence_s: float = 0.8
    # Give up on a turn that never completes, so one stuck turn cannot wedge a run.
    turn_timeout_s: float = 30.0
    connect_timeout_s: float = 15.0
    record_events: bool = True


@dataclass
class SessionResult:
    session_index: int
    session_id: str
    turns: list[TurnTimeline] = field(default_factory=list)
    events: list[RecordedEvent] = field(default_factory=list)
    connect_ms: float | None = None
    rejected: str | None = None
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "session_index": self.session_index,
            "session_id": self.session_id,
            "connect_ms": self.connect_ms,
            "rejected": self.rejected,
            "error": self.error,
            "turns": [turn.to_json() for turn in self.turns],
        }


class RealtimeProbe:
    """Drives one Realtime conversation and records its timeline."""

    def __init__(self, config: SessionConfig, session_index: int = 0) -> None:
        self.config = config
        self.session_index = session_index
        self.result = SessionResult(session_index=session_index, session_id=f"probe-{session_index}")
        self.builder = TimelineBuilder(session_id=self.result.session_id)
        self._socket: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._turn_done = asyncio.Event()
        self._closed = asyncio.Event()

    # ── Connection ───────────────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def connect(self) -> AsyncIterator["RealtimeProbe"]:
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        started = time.monotonic()
        try:
            self._socket = await asyncio.wait_for(
                websockets.connect(self.config.url, additional_headers=headers, max_size=None),
                timeout=self.config.connect_timeout_s,
            )
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
            self.result.error = f"{type(exc).__name__}: {exc}"
            raise
        self.result.connect_ms = (time.monotonic() - started) * 1000.0
        self._reader = asyncio.create_task(self._read_loop(), name=f"probe-read-{self.session_index}")
        try:
            try:
                await self._send({"type": "session.update", "session": self._session_payload()})
            except websockets.ConnectionClosed:
                # A server with no free slot sends session_limit_reached and hangs
                # up at once. Let the reader record that reason before tearing
                # down, or a refusal is counted as an anonymous failure.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(self._reader), timeout=2.0)
                raise
            yield self
        finally:
            await self.close()

    def _session_payload(self) -> dict[str, Any]:
        audio: dict[str, Any] = {
            "input": {"turn_detection": {"type": "server_vad", "interrupt_response": True}},
            "output": {},
        }
        # 16 kHz is the pipeline's native rate and is selected by omitting the
        # format entirely; only 24 kHz is spelled out in the OpenAI PCM schema.
        if self.config.send_rate == 24_000:
            audio["input"]["format"] = {"type": "audio/pcm", "rate": 24_000}
            audio["output"]["format"] = {"type": "audio/pcm", "rate": 24_000}
        elif self.config.send_rate != 16_000:
            raise ValueError(f"send_rate must be 16000 or 24000, got {self.config.send_rate}")
        if self.config.voice:
            audio["output"]["voice"] = self.config.voice
        session: dict[str, Any] = {"type": "realtime", "audio": audio}
        if self.config.instructions:
            session["instructions"] = self.config.instructions
        return session

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()
            self._socket = None

    # ── Receive ──────────────────────────────────────────────────────────

    async def _read_loop(self) -> None:
        assert self._socket is not None
        try:
            async for raw in self._socket:
                # Stamp arrival before parsing. JSON decoding of a large audio
                # delta is not free, and charging it to the server would inflate
                # exactly the numbers this tool exists to report.
                now = time.monotonic()
                wall = time.time()
                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                event = RecordedEvent(t=now, wall=wall, type=str(payload.get("type", "")), payload=payload)
                self._handle(event)
        except asyncio.CancelledError:
            raise
        except websockets.WebSocketException as exc:
            self.result.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._closed.set()
            self._turn_done.set()

    def _handle(self, event: RecordedEvent) -> None:
        if self.config.record_events:
            self.result.events.append(event)
        self.builder.add(event)

        if event.type == "session.created":
            session = event.payload.get("session") or {}
            session_id = session.get("id")
            if isinstance(session_id, str):
                self.result.session_id = session_id
                self.builder.session_id = session_id
        elif event.type == "error":
            error = event.payload.get("error") or {}
            if error.get("type") == "session_limit_reached":
                self.result.rejected = "session_limit_reached"
                self._turn_done.set()
        elif event.type == "response.done":
            self._turn_done.set()

    # ── Send ─────────────────────────────────────────────────────────────

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._socket is None:
            raise RuntimeError("probe is not connected")
        await self._socket.send(json.dumps(payload))

    async def _stream_pcm(self, pcm: bytes, realtime: bool = True) -> None:
        """Push PCM as 20 ms frames, paced against the wall clock.

        Pacing uses an absolute deadline per frame rather than sleeping a fixed
        interval, so scheduling jitter does not accumulate into drift over a long
        utterance.
        """

        frames = chunk_pcm(pcm, FRAME_MS, self.config.send_rate)
        deadline = time.monotonic()
        for frame in frames:
            if not frame:
                continue
            await self._send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(frame).decode("ascii"),
                }
            )
            if realtime:
                deadline += FRAME_MS / 1000.0
                delay = deadline - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)

    # ── Turns ────────────────────────────────────────────────────────────

    async def speak(self, prompt: Prompt) -> TurnTimeline:
        """Speak one prompt and wait for the reply to finish."""

        self._turn_done.clear()
        turn = self.builder.mark_client_speech_start(time.monotonic(), prompt=prompt.text)
        await self._stream_pcm(prompt.pcm)
        self.builder.mark_client_speech_end(time.monotonic())
        # Trailing silence is what actually triggers server-side VAD; it is part
        # of the utterance from the server's point of view, so it is sent at the
        # same real-time pace.
        await self._stream_pcm(silence(self.config.trailing_silence_s, self.config.send_rate))

        try:
            await asyncio.wait_for(self._turn_done.wait(), timeout=self.config.turn_timeout_s)
        except asyncio.TimeoutError:
            turn.errors.append("turn_timeout")
            turn.status = turn.status or "timeout"
            logger.warning("session %d turn %d timed out", self.session_index, turn.turn_index)
        return turn

    async def barge_in(self, prompt: Prompt, after_s: float) -> TurnTimeline | None:
        """Start speaking over the assistant mid-reply and time the cancellation.

        ``after_s`` is measured from the first audio frame received, not from the
        request, so the interruption lands while the assistant is genuinely
        speaking rather than while it is still thinking.
        """

        interrupted = await self._await_speaking(timeout_s=self.config.turn_timeout_s)
        if interrupted is None:
            logger.warning("session %d had nothing to interrupt", self.session_index)
            return None
        await asyncio.sleep(after_s)
        self.builder.mark_barge_in(time.monotonic())
        await self._stream_pcm(prompt.pcm)
        self.builder.mark_client_speech_end(time.monotonic())
        await self._stream_pcm(silence(self.config.trailing_silence_s, self.config.send_rate))
        return interrupted

    async def _await_speaking(self, timeout_s: float) -> TurnTimeline | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for turn in reversed(self.builder.turns):
                if turn.first_audio is not None and turn.response_done is None:
                    return turn
            await asyncio.sleep(0.01)
        return None

    async def converse(self, prompts: PromptLibrary, turns: int, interval_s: float = 0.0) -> SessionResult:
        """Run ``turns`` sequential turns, optionally at a fixed cadence."""

        for turn_index in range(turns):
            if self.result.rejected or self._closed.is_set():
                break
            started = time.monotonic()
            prompt = prompts.for_session(self.session_index, turn_index)
            await self.speak(prompt)
            if interval_s > 0:
                remaining = interval_s - (time.monotonic() - started)
                if remaining > 0:
                    await asyncio.sleep(remaining)
        self.result.turns = self.builder.turns
        return self.result


def write_trace(path: Path, result: SessionResult) -> None:
    """Persist the raw event timeline so every published number can be recomputed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in result.events:
            handle.write(json.dumps(event.to_json()) + "\n")
