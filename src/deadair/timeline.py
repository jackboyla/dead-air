"""Reduce an OpenAI Realtime event stream into per-turn latency measurements.

The pipeline under test is ``VAD -> STT -> LLM -> TTS``. Every stage boundary is
already visible on the wire as a Realtime event, so a client can measure the whole
budget without instrumenting the server. That is deliberate: the same reducer runs
against this local stack, against hosted OpenAI, or against any compatible server,
and the numbers stay comparable.

Two clocks matter and they are not the same clock:

``client_speech_end``
    When the probe stopped sending speech samples. This is when a human would
    have stopped talking.

``input_audio_buffer.speech_stopped``
    When the server's VAD decided the human stopped talking. It necessarily lags
    the first clock by the VAD's silence window plus padding.

Reporting only from the server's clock hides that lag, which on a default
configuration is one of the largest single terms in the budget. Both are kept.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

# Event names that carry the first token of model output. A server may surface
# LLM progress as text deltas, as audio-transcript deltas, or both, depending on
# whether live transcription is enabled. The earliest of either is the first
# evidence the client has that the language model has started producing.
FIRST_TOKEN_EVENTS = frozenset(
    {
        "response.output_text.delta",
        "response.output_audio_transcript.delta",
    }
)

TERMINAL_RESPONSE_STATUSES = frozenset({"completed", "cancelled", "failed", "incomplete"})


def _ms(later: float | None, earlier: float | None) -> float | None:
    """Milliseconds between two monotonic timestamps, or None if either is missing."""

    if later is None or earlier is None:
        return None
    return (later - earlier) * 1000.0


@dataclass
class RecordedEvent:
    """One Realtime event, stamped on arrival at the client.

    ``t`` is a monotonic timestamp taken the moment the frame was read off the
    socket, before any parsing. ``wall`` is only for correlating with server logs
    and external traces; never do arithmetic with it.
    """

    t: float
    wall: float
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"t": self.t, "wall": self.wall, "type": self.type, "payload": self.payload}


@dataclass
class TurnTimeline:
    """Stage boundaries and derived latencies for a single conversational turn.

    A turn is keyed by ``response_id``. Timestamps are monotonic seconds; every
    derived field is milliseconds, or None when the turn never reached that stage.
    """

    session_id: str
    turn_index: int
    response_id: str | None = None

    # ── Stage boundaries, monotonic seconds ──────────────────────────────
    client_speech_start: float | None = None
    client_speech_end: float | None = None
    speech_started: float | None = None
    speech_stopped: float | None = None
    transcript_done: float | None = None
    response_created: float | None = None
    first_token: float | None = None
    first_audio: float | None = None
    audio_done: float | None = None
    response_done: float | None = None

    # ── Barge-in ─────────────────────────────────────────────────────────
    barge_in_sent: float | None = None
    last_audio_after_barge_in: float | None = None

    # ── Outcome ──────────────────────────────────────────────────────────
    status: str | None = None
    prompt: str | None = None
    transcript: str | None = None
    response_text: str = ""
    audio_bytes: int = 0
    audio_sample_rate: int = 24_000
    errors: list[str] = field(default_factory=list)
    # Events that break the Realtime contract even when every latency looks fine.
    protocol_violations: list[str] = field(default_factory=list)

    @property
    def audio_duration_ms(self) -> float:
        """Duration of synthesized audio received, assuming 16-bit mono PCM."""

        return self.audio_bytes / 2.0 / self.audio_sample_rate * 1000.0

    # ── Derived latencies ────────────────────────────────────────────────

    @property
    def vad_eou_lag_ms(self) -> float | None:
        """How long the VAD took to agree the user had stopped talking.

        Charged to the user's perceived wait even though no model has run yet.
        """

        return _ms(self.speech_stopped, self.client_speech_end)

    @property
    def asr_ms(self) -> float | None:
        """End of detected speech to final transcript."""

        return _ms(self.transcript_done, self.speech_stopped)

    @property
    def llm_ttft_ms(self) -> float | None:
        """Final transcript to first token of model output, as seen by the client.

        This includes the server's own turn-taking decision and any queue wait, so
        it is an upper bound on the language model's own time to first token. Pair
        it with the tap's server-side measurement to split the two.
        """

        return _ms(self.first_token, self.transcript_done)

    @property
    def tts_ttfb_ms(self) -> float | None:
        """First token of model output to first byte of synthesized audio."""

        return _ms(self.first_audio, self.first_token)

    @property
    def ttfa_ms(self) -> float | None:
        """Detected end of speech to first audio. The server-side headline number."""

        return _ms(self.first_audio, self.speech_stopped)

    @property
    def perceived_ttfa_ms(self) -> float | None:
        """Actual end of speech to first audio. What the user experiences."""

        return _ms(self.first_audio, self.client_speech_end)

    @property
    def response_total_ms(self) -> float | None:
        return _ms(self.response_done, self.response_created)

    @property
    def barge_in_ms(self) -> float | None:
        """Barge-in sent to the last audio frame the client still had to play.

        Measured against the final audio delta rather than ``response.done``,
        because audio that arrives after the interruption is audio the user hears
        over their own voice. A cancel that is fast on paper but keeps emitting
        audio is not a fast cancel.
        """

        return _ms(self.last_audio_after_barge_in, self.barge_in_sent)

    @property
    def realtime_factor(self) -> float | None:
        """Synthesized audio duration divided by wall time spent producing it.

        Above 1.0 means the pipeline generates speech faster than it is spoken,
        which is the condition for sustained conversation without underrun.
        """

        produced = _ms(self.audio_done or self.response_done, self.first_audio)
        if produced is None or produced <= 0 or self.audio_bytes == 0:
            return None
        return self.audio_duration_ms / produced

    @property
    def completed(self) -> bool:
        return self.status == "completed" and self.first_audio is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "response_id": self.response_id,
            "status": self.status,
            "prompt": self.prompt,
            "transcript": self.transcript,
            "response_text": self.response_text,
            "audio_bytes": self.audio_bytes,
            "audio_duration_ms": round(self.audio_duration_ms, 2),
            "errors": self.errors,
            "protocol_violations": self.protocol_violations,
            "latency_ms": {
                "vad_eou_lag": self.vad_eou_lag_ms,
                "asr": self.asr_ms,
                "llm_ttft": self.llm_ttft_ms,
                "tts_ttfb": self.tts_ttfb_ms,
                "ttfa": self.ttfa_ms,
                "perceived_ttfa": self.perceived_ttfa_ms,
                "response_total": self.response_total_ms,
                "barge_in": self.barge_in_ms,
            },
            "realtime_factor": self.realtime_factor,
        }


class TimelineBuilder:
    """Fold a session's Realtime events into one :class:`TurnTimeline` per turn.

    The builder is fed in arrival order and never looks ahead, so it behaves the
    same live as it does replaying a trace file.

    Turn boundaries are a genuine problem here rather than a formality. The server
    may speculatively open a response and later supersede it, so ``response.created``
    is not a reliable one-to-one turn marker. The builder therefore opens a turn on
    the first *speech* event and binds it to whichever ``response_id`` the server
    settles on, discarding superseded ids rather than emitting phantom turns.
    """

    def __init__(self, session_id: str = "unknown") -> None:
        self.session_id = session_id
        self.turns: list[TurnTimeline] = []
        self._open: TurnTimeline | None = None
        self._by_response: dict[str, TurnTimeline] = {}

    # ── Client-side marks ────────────────────────────────────────────────

    def mark_client_speech_start(self, t: float, prompt: str | None = None) -> TurnTimeline:
        """Open a turn at the moment the probe starts sending speech."""

        turn = self._open
        if turn is None or turn.client_speech_start is not None:
            turn = self._new_turn()
        turn.client_speech_start = t
        turn.prompt = prompt
        return turn

    def mark_client_speech_end(self, t: float) -> None:
        if self._open is not None:
            self._open.client_speech_end = t

    def mark_barge_in(self, t: float) -> None:
        """Record that the probe started interrupting the turn currently speaking."""

        target = self._speaking_turn()
        if target is not None:
            target.barge_in_sent = t

    def _speaking_turn(self) -> TurnTimeline | None:
        for turn in reversed(self.turns):
            if turn.first_audio is not None and turn.response_done is None:
                return turn
        return None

    def _new_turn(self) -> TurnTimeline:
        turn = TurnTimeline(session_id=self.session_id, turn_index=len(self.turns))
        self.turns.append(turn)
        self._open = turn
        return turn

    def _current(self) -> TurnTimeline:
        if self._open is None:
            return self._new_turn()
        return self._open

    def _for_response(self, response_id: str | None) -> TurnTimeline:
        """Resolve the turn a response-scoped event belongs to.

        Binds the id to the open turn on first sight. An id we have never seen and
        cannot bind gets its own turn, so stray output is counted rather than
        silently dropped.
        """

        if response_id is not None and response_id in self._by_response:
            return self._by_response[response_id]
        turn = self._current()
        if response_id is not None:
            if turn.response_id is not None and turn.response_id != response_id:
                turn = self._new_turn()
            turn.response_id = response_id
            self._by_response[response_id] = turn
        return turn

    # ── Server events ────────────────────────────────────────────────────

    def add(self, event: RecordedEvent) -> None:
        handler = _HANDLERS.get(event.type)
        if handler is not None:
            handler(self, event)

    def extend(self, events: Iterable[RecordedEvent]) -> None:
        for event in events:
            self.add(event)

    def _on_speech_started(self, event: RecordedEvent) -> None:
        turn = self._current()
        if turn.speech_started is None:
            turn.speech_started = event.t

    def _on_speech_stopped(self, event: RecordedEvent) -> None:
        turn = self._current()
        turn.speech_stopped = event.t

    def _on_transcript_done(self, event: RecordedEvent) -> None:
        turn = self._current()
        turn.transcript_done = event.t
        transcript = event.payload.get("transcript")
        if isinstance(transcript, str):
            turn.transcript = transcript

    def _on_transcript_failed(self, event: RecordedEvent) -> None:
        turn = self._current()
        turn.errors.append("input_audio_transcription.failed")

    def _on_response_created(self, event: RecordedEvent) -> None:
        response = event.payload.get("response") or {}
        turn = self._for_response(response.get("id"))
        if turn.response_created is None:
            turn.response_created = event.t

    def _on_output_delta(self, event: RecordedEvent) -> None:
        turn = self._for_response(event.payload.get("response_id"))
        if turn.first_token is None:
            turn.first_token = event.t
        delta = event.payload.get("delta")
        if isinstance(delta, str):
            turn.response_text += delta

    def _on_audio_delta(self, event: RecordedEvent) -> None:
        turn = self._for_response(event.payload.get("response_id"))
        # Audio after the terminal event is played over whatever comes next, and
        # a client that trusts response.done has already stopped listening for it.
        if turn.response_done is not None:
            turn.protocol_violations.append("audio_after_response_done")
        elif turn.response_created is None:
            turn.protocol_violations.append("audio_before_response_created")
        if turn.first_audio is None:
            turn.first_audio = event.t
        # A server that streams audio without exposing text deltas still tells us
        # the model produced something; treat audio as first-token evidence too.
        if turn.first_token is None:
            turn.first_token = event.t
        turn.audio_bytes += _decoded_len(event.payload.get("delta"))
        if turn.barge_in_sent is not None:
            turn.last_audio_after_barge_in = event.t

    def _on_audio_done(self, event: RecordedEvent) -> None:
        turn = self._for_response(event.payload.get("response_id"))
        turn.audio_done = event.t

    def _on_response_done(self, event: RecordedEvent) -> None:
        response = event.payload.get("response") or {}
        turn = self._for_response(response.get("id"))
        turn.response_done = event.t
        status = response.get("status")
        if isinstance(status, str):
            turn.status = status
        if status == "failed":
            details = response.get("status_details") or {}
            error = details.get("error") or {}
            turn.errors.append(str(error.get("message") or "response.failed"))
        # A barge-in that never produced a trailing audio frame still cancelled
        # cleanly; charge the cancel to response.done so the metric exists.
        if turn.barge_in_sent is not None and turn.last_audio_after_barge_in is None:
            turn.last_audio_after_barge_in = event.t
        if turn is self._open:
            self._open = None

    def _on_error(self, event: RecordedEvent) -> None:
        error = event.payload.get("error") or {}
        message = str(error.get("type") or error.get("message") or "error")
        self._current().errors.append(message)


def _decoded_len(delta: Any) -> int:
    """Length in bytes of a base64 audio delta, without decoding it.

    Turn counts run to thousands of deltas per session; decoding every one just to
    measure it wastes the probe's event loop on work that arithmetic answers.
    """

    if not isinstance(delta, str) or not delta:
        return 0
    padding = delta.count("=", -2)
    return (len(delta) * 3) // 4 - padding


_HANDLERS = {
    "input_audio_buffer.speech_started": TimelineBuilder._on_speech_started,
    "input_audio_buffer.speech_stopped": TimelineBuilder._on_speech_stopped,
    "conversation.item.input_audio_transcription.completed": TimelineBuilder._on_transcript_done,
    "conversation.item.input_audio_transcription.failed": TimelineBuilder._on_transcript_failed,
    "response.created": TimelineBuilder._on_response_created,
    "response.output_text.delta": TimelineBuilder._on_output_delta,
    "response.output_audio_transcript.delta": TimelineBuilder._on_output_delta,
    "response.output_audio.delta": TimelineBuilder._on_audio_delta,
    "response.output_audio.done": TimelineBuilder._on_audio_done,
    "response.done": TimelineBuilder._on_response_done,
    "error": TimelineBuilder._on_error,
}


def replay(events: Sequence[RecordedEvent], session_id: str = "replay") -> list[TurnTimeline]:
    """Rebuild turns from a recorded trace. Used by tests and by ``deadair report``."""

    builder = TimelineBuilder(session_id=session_id)
    builder.extend(events)
    return builder.turns


def iter_events(raw: Iterable[dict[str, Any]]) -> Iterator[RecordedEvent]:
    """Parse JSONL trace rows back into events."""

    for row in raw:
        yield RecordedEvent(
            t=float(row["t"]),
            wall=float(row.get("wall", 0.0)),
            type=str(row["type"]),
            payload=row.get("payload") or {},
        )
