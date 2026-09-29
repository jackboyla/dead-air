"""A deterministic Realtime server for testing the probe without models or GPUs.

It speaks the same event sequence the pipeline does — speech started, speech
stopped, transcript, response, audio, done — with fixed, configurable stage
delays. That makes it useful in two ways:

**CI.** The probe, the reducer, the report and the budget gate can run end to
end on a laptop or a hosted runner, so a change that breaks measurement is caught
before it reaches a GPU box.

**Negative drills.** A measurement tool is only trustworthy if it notices when
things go wrong. The mock can fail every Nth response, refuse sessions past a
limit, and send audio after it has declared a response cancelled, so each of those
detectors is exercised against a known answer.

Turn detection is a simple energy gate: frames with any sample above
``voiced_threshold`` count as speech, and ``speech_stopped`` fires once
``vad_silence_ms`` of silent audio has arrived. Silence is counted in audio time,
as a real VAD counts samples, not in wall time since the last voiced frame. The probe's trailing silence therefore
ends the turn the same way it does against the real pipeline, and speaking over
a reply interrupts it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import numpy as np
from websockets.asyncio.server import ServerConnection, serve

logger = logging.getLogger("deadair.mock")


@dataclass(frozen=True)
class MockConfig:
    host: str = "127.0.0.1"
    port: int = 18766
    # Quiet time after the last voiced frame before the turn is declared over.
    vad_silence_ms: int = 200
    voiced_threshold: int = 500
    stt_ms: int = 40
    llm_ms: int = 60
    tts_ms: int = 30
    audio_chunks: int = 10
    audio_chunk_ms: int = 100
    chunk_gap_ms: int = 40
    output_rate: int = 24_000
    # 0 means unlimited. Sessions past the limit get session_limit_reached.
    max_sessions: int = 0
    # 0 disables. Otherwise every Nth turn across the server ends in failure.
    fail_every: int = 0
    # Send one audio delta after reporting a response cancelled.
    stale_audio_after_cancel: bool = False


class MockRealtimeServer:
    def __init__(self, config: MockConfig) -> None:
        self.config = config
        self.turn_count = 0
        self.active_sessions = 0

    async def handler(self, websocket: ServerConnection) -> None:
        if self.config.max_sessions and self.active_sessions >= self.config.max_sessions:
            await _send(
                websocket,
                {
                    "type": "error",
                    "error": {"type": "session_limit_reached", "message": "No free pipeline slot."},
                },
            )
            await websocket.close(code=1008, reason="All session slots are in use")
            return
        self.active_sessions += 1
        try:
            await _Session(self, websocket).run()
        finally:
            self.active_sessions -= 1


class _Session:
    """State for one connection. Mirrors the pipeline's one-response-at-a-time rule."""

    def __init__(self, server: MockRealtimeServer, websocket: ServerConnection) -> None:
        self.server = server
        self.config = server.config
        self.websocket = websocket
        self.speech_active = False
        self.silence_ms = 0.0
        self.input_rate = 16_000
        self.finalize_task: asyncio.Task[None] | None = None
        self.response_task: asyncio.Task[None] | None = None
        self.response_id: str | None = None

    async def run(self) -> None:
        session_id = f"sess_{uuid.uuid4().hex[:8]}"
        await _send(self.websocket, {"type": "session.created", "session": {"id": session_id, "type": "realtime"}})
        try:
            async for raw in self.websocket:
                if not isinstance(raw, str):
                    continue
                event = json.loads(raw)
                await self._on_event(event)
        finally:
            tasks = [task for task in (self.finalize_task, self.response_task) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _on_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "session.update":
            session = event.get("session") or {}
            rate = ((session.get("audio") or {}).get("input") or {}).get("format", {}).get("rate")
            if isinstance(rate, int):
                self.input_rate = rate
            await _send(self.websocket, {"type": "session.updated", "session": session})
        elif event_type == "input_audio_buffer.append":
            samples = _samples(event.get("audio"))
            if samples.size and int(np.abs(samples.astype(np.int32)).max()) > self.config.voiced_threshold:
                self.silence_ms = 0.0
                await self._on_speech()
            elif self.speech_active:
                self.silence_ms += samples.size / self.input_rate * 1000.0
                if self.silence_ms >= self.config.vad_silence_ms:
                    self.speech_active = False
                    self.finalize_task = asyncio.create_task(self._finalize())
        elif event_type == "response.cancel":
            await self._cancel_response("client_cancelled")
        else:
            await _send(
                self.websocket,
                {"type": "error", "error": {"type": "invalid_request_error", "message": f"unsupported {event_type}"}},
            )

    async def _on_speech(self) -> None:
        if self._responding():
            await self._cancel_response("turn_detected")
        if not self.speech_active:
            self.speech_active = True
            await _send(self.websocket, {"type": "input_audio_buffer.speech_started"})

    def _responding(self) -> bool:
        return self.response_task is not None and not self.response_task.done()

    async def _finalize(self) -> None:
        self.server.turn_count += 1
        item_id = f"item_{uuid.uuid4().hex[:8]}"
        await _send(self.websocket, {"type": "input_audio_buffer.speech_stopped", "item_id": item_id})
        await asyncio.sleep(self.config.stt_ms / 1000)
        await _send(
            self.websocket,
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": item_id,
                "transcript": "mock utterance",
            },
        )
        response_id = f"resp_{uuid.uuid4().hex[:8]}"
        self.response_id = response_id
        await _send(
            self.websocket, {"type": "response.created", "response": {"id": response_id, "status": "in_progress"}}
        )
        if self.config.fail_every and self.server.turn_count % self.config.fail_every == 0:
            await _send(
                self.websocket,
                {
                    "type": "response.done",
                    "response": {
                        "id": response_id,
                        "status": "failed",
                        "status_details": {"type": "failed", "error": {"message": "mock failure"}},
                        "output": [],
                    },
                },
            )
            return
        self.response_task = asyncio.create_task(self._generate(response_id))

    async def _generate(self, response_id: str) -> None:
        await asyncio.sleep(self.config.llm_ms / 1000)
        item_id = f"out_{uuid.uuid4().hex[:8]}"
        await _send(
            self.websocket,
            {
                "type": "response.output_audio_transcript.delta",
                "response_id": response_id,
                "item_id": item_id,
                "delta": "Mock response.",
            },
        )
        await asyncio.sleep(self.config.tts_ms / 1000)
        for _ in range(self.config.audio_chunks):
            await self._send_audio(response_id)
            await asyncio.sleep(self.config.chunk_gap_ms / 1000)
        await _send(
            self.websocket, {"type": "response.output_audio.done", "response_id": response_id, "item_id": item_id}
        )
        await _send(
            self.websocket,
            {"type": "response.done", "response": {"id": response_id, "status": "completed", "output": []}},
        )

    async def _cancel_response(self, reason: str) -> None:
        if not self._responding() or self.response_id is None:
            return
        assert self.response_task is not None
        self.response_task.cancel()
        await asyncio.gather(self.response_task, return_exceptions=True)
        response_id = self.response_id
        await _send(
            self.websocket,
            {
                "type": "response.done",
                "response": {
                    "id": response_id,
                    "status": "cancelled",
                    "status_details": {"type": "cancelled", "reason": reason},
                    "output": [],
                },
            },
        )
        if self.config.stale_audio_after_cancel:
            await self._send_audio(response_id)

    async def _send_audio(self, response_id: str) -> None:
        silence = b"\x00\x00" * (self.config.output_rate * self.config.audio_chunk_ms // 1000)
        await _send(
            self.websocket,
            {
                "type": "response.output_audio.delta",
                "response_id": response_id,
                "delta": base64.b64encode(silence).decode("ascii"),
            },
        )


def _samples(audio: Any) -> np.ndarray:
    if not isinstance(audio, str) or not audio:
        return np.zeros(0, dtype="<i2")
    return np.frombuffer(base64.b64decode(audio), dtype="<i2")


async def _send(websocket: ServerConnection, event: dict[str, Any]) -> None:
    await websocket.send(json.dumps(event, separators=(",", ":")))


@contextlib.asynccontextmanager
async def running(config: MockConfig) -> AsyncIterator[str]:
    """Serve the mock for the duration of a block and yield its URL. Port 0 picks a free port."""

    server = MockRealtimeServer(config)
    async with serve(server.handler, config.host, config.port, max_size=None) as ws_server:
        port = next(iter(ws_server.sockets)).getsockname()[1]
        yield f"ws://{config.host}:{port}/v1/realtime"


async def serve_mock(config: MockConfig) -> None:
    async with running(config) as url:
        print(f"mock Realtime server listening on {url}", flush=True)
        await asyncio.Future()
