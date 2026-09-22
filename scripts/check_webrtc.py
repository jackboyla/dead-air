#!/usr/bin/env python3
"""Verify the WebRTC path the browser client uses, without a browser.

Performs the same handshake as ``web/app.js``: POST an SDP offer as
``application/sdp`` to ``/v1/realtime/calls``, apply the answer, then speak a
prompt over the media track and read events off the ``oai-events`` data channel.

Worth having as a script rather than a test: it needs a running server with GPUs
and models, so it cannot go in CI, but "does WebRTC still work" is the question
you want answered in one command before demoing anything.

Needs aiortc::

    ../speech-to-speech/.venv/bin/python scripts/check_webrtc.py
"""

from __future__ import annotations

import argparse
import asyncio
import fractions
import json
import sys
import time
import wave
from pathlib import Path

import httpx
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from av import AudioFrame

SAMPLE_RATE = 48_000  # WebRTC media is negotiated at 48 kHz
FRAME_SAMPLES = 960  # 20 ms


class PromptTrack(MediaStreamTrack):
    """Sends one prompt, then silence, paced in real time."""

    kind = "audio"

    def __init__(self, samples: np.ndarray, tail_s: float = 6.0) -> None:
        super().__init__()
        tail = np.zeros(int(SAMPLE_RATE * tail_s), dtype=np.int16)
        self._samples = np.concatenate([samples, tail])
        self._offset = 0
        self._timestamp = 0
        self._start = time.monotonic()

    async def recv(self) -> AudioFrame:
        if self._offset >= len(self._samples):
            raise MediaStreamError

        # Pace against the wall clock; the server's VAD expects microphone timing.
        target = self._start + self._timestamp / SAMPLE_RATE
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        chunk = self._samples[self._offset : self._offset + FRAME_SAMPLES]
        if len(chunk) < FRAME_SAMPLES:
            chunk = np.pad(chunk, (0, FRAME_SAMPLES - len(chunk)))
        self._offset += FRAME_SAMPLES

        frame = AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        self._timestamp += FRAME_SAMPLES
        return frame


def load_prompt(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if rate != SAMPLE_RATE:
        import soxr

        samples = soxr.resample(samples, rate, SAMPLE_RATE)
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)


async def run(base_url: str, prompt_path: Path, timeout_s: float) -> int:
    samples = load_prompt(prompt_path)
    pc = RTCPeerConnection()
    events: list[dict] = []
    audio_frames = 0
    first_audio_at: float | None = None
    done = asyncio.Event()

    channel = pc.createDataChannel("oai-events")

    @channel.on("open")
    def _on_open() -> None:
        channel.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "audio": {
                            "input": {"turn_detection": {"type": "server_vad", "interrupt_response": True}},
                            "output": {},
                        },
                        "instructions": "You are a voice assistant. Answer in one short sentence.",
                    },
                }
            )
        )

    @channel.on("message")
    def _on_message(message: str) -> None:
        try:
            event = json.loads(message)
        except ValueError:
            return
        events.append(event)
        kind = event.get("type", "")
        if kind not in ("response.output_audio.delta", "response.output_audio_transcript.delta"):
            print(f"  event  {kind}")
        if kind == "conversation.item.input_audio_transcription.completed":
            print(f"  heard  {event.get('transcript')!r}")
        if kind == "response.output_audio_transcript.done":
            print(f"  said   {event.get('transcript')!r}")
        if kind == "response.done":
            done.set()

    @pc.on("track")
    def _on_track(track: MediaStreamTrack) -> None:
        async def drain() -> None:
            nonlocal audio_frames, first_audio_at
            while True:
                try:
                    await track.recv()
                except MediaStreamError:
                    return
                if first_audio_at is None:
                    first_audio_at = time.monotonic()
                audio_frames += 1

        asyncio.ensure_future(drain())

    pc.addTrack(PromptTrack(samples))

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    print(f"POST {base_url}/v1/realtime/calls")
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{base_url}/v1/realtime/calls",
            content=pc.localDescription.sdp,
            headers={"Content-Type": "application/sdp"},
        )
    # The GA calls endpoint answers 201 Created with a Location header, not 200.
    if not response.is_success:
        print(f"  handshake failed: {response.status_code} {response.text[:200]}", file=sys.stderr)
        await pc.close()
        return 1

    call_id = (response.headers.get("Location") or "").rsplit("/", 1)[-1]
    print(f"  answer received, call id {call_id or '(none)'}")
    await pc.setRemoteDescription(RTCSessionDescription(sdp=response.text, type="answer"))

    try:
        await asyncio.wait_for(done.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        print("  timed out waiting for response.done", file=sys.stderr)

    await pc.close()
    if call_id:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.delete(f"{base_url}/v1/realtime/calls/{call_id}")

    ok = audio_frames > 0 and any(e.get("type") == "response.done" for e in events)
    print(f"\n  events received: {len(events)}")
    print(f"  audio frames received: {audio_frames}")
    print("  RESULT: " + ("WebRTC path works" if ok else "WebRTC path did NOT complete"))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:18765")
    parser.add_argument("--prompt", type=Path, default=Path("assets/prompts/capital_france.wav"))
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args()
    return asyncio.run(run(args.url.rstrip("/"), args.prompt, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
