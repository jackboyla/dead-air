"""Fault injection for the LLM backend.

Every failure here is one a real deployment meets: the backend is slow to start,
the backend stalls halfway through a sentence, the backend returns 503 under load,
the backend hangs up mid-stream. None of them are interesting as unit tests. They
are interesting because of what the *voice* does when they happen, which you can
only learn by making them happen to a live conversation and listening.

Specs are written as short strings so they can be passed on a command line or
posted to the running tap:

===========================  ================================================
``slow-start:1200ms``        Hold the request 1200 ms before contacting upstream.
``slow-start:200-900ms``     Hold it a uniformly random time in that range.
``stall:400ms@3``            Pause 400 ms after relaying chunk 3.
``truncate@12``              Hang up after relaying chunk 12.
``reject:503``               Answer 503 without contacting upstream.
===========================  ================================================

Any spec may carry a probability suffix, ``reject:503;p=0.1``, so a fault can be
made intermittent. Intermittent is usually the honest setting: a backend that
fails every time gets noticed immediately, and one that fails a tenth of the time
is the one that reaches production.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable
from dataclasses import dataclass, field

_DURATION = re.compile(r"^(?P<low>\d+(?:\.\d+)?)(?:-(?P<high>\d+(?:\.\d+)?))?(?P<unit>ms|s)$")


class FaultSpecError(ValueError):
    """Raised for a malformed fault specification."""


def _parse_duration(text: str) -> tuple[float, float]:
    """Parse ``400ms`` or ``0.2-1.5s`` into a (low, high) pair of seconds."""

    match = _DURATION.match(text.strip())
    if match is None:
        raise FaultSpecError(f"bad duration {text!r}; expected forms like 400ms, 1.5s, 200-900ms")
    scale = 0.001 if match.group("unit") == "ms" else 1.0
    low = float(match.group("low")) * scale
    high = float(match.group("high")) * scale if match.group("high") else low
    if high < low:
        raise FaultSpecError(f"bad duration {text!r}; upper bound is below lower bound")
    return low, high


@dataclass(frozen=True)
class FaultSpec:
    """One parsed fault rule."""

    kind: str
    low: float = 0.0
    high: float = 0.0
    at_chunk: int | None = None
    status: int = 503
    probability: float = 1.0
    raw: str = ""

    KINDS = ("slow-start", "stall", "truncate", "reject")

    @classmethod
    def parse(cls, text: str) -> "FaultSpec":
        raw = text.strip()
        if not raw:
            raise FaultSpecError("empty fault specification")

        probability = 1.0
        body = raw
        if ";" in body:
            body, _, tail = body.partition(";")
            for part in tail.split(";"):
                part = part.strip()
                if not part:
                    continue
                key, _, value = part.partition("=")
                if key.strip() != "p":
                    raise FaultSpecError(f"unknown fault option {part!r}; only p= is supported")
                try:
                    probability = float(value)
                except ValueError as exc:
                    raise FaultSpecError(f"bad probability in {raw!r}") from exc
                if not 0.0 <= probability <= 1.0:
                    raise FaultSpecError(f"probability must be within [0, 1], got {probability}")

        at_chunk: int | None = None
        if "@" in body:
            body, _, chunk_text = body.partition("@")
            try:
                at_chunk = int(chunk_text)
            except ValueError as exc:
                raise FaultSpecError(f"bad chunk index in {raw!r}") from exc
            if at_chunk < 0:
                raise FaultSpecError(f"chunk index must not be negative in {raw!r}")

        kind, _, argument = body.strip().partition(":")
        kind = kind.strip()
        if kind not in cls.KINDS:
            raise FaultSpecError(f"unknown fault {kind!r}; choose from {', '.join(cls.KINDS)}")

        if kind == "reject":
            status = int(argument) if argument else 503
            return cls(kind=kind, status=status, probability=probability, raw=raw)

        if kind == "truncate":
            if at_chunk is None:
                raise FaultSpecError("truncate needs a chunk index, for example truncate@12")
            return cls(kind=kind, at_chunk=at_chunk, probability=probability, raw=raw)

        if not argument:
            raise FaultSpecError(f"{kind} needs a duration, for example {kind}:400ms")
        low, high = _parse_duration(argument)
        if kind == "stall" and at_chunk is None:
            at_chunk = 0
        return cls(kind=kind, low=low, high=high, at_chunk=at_chunk, probability=probability, raw=raw)

    def sample_delay(self, rng: random.Random) -> float:
        if self.high <= self.low:
            return self.low
        return rng.uniform(self.low, self.high)


@dataclass
class FaultDecision:
    """The faults drawn for one request, resolved up front.

    Drawing once at request start rather than re-rolling per chunk keeps a single
    request internally consistent: it either stalls or it does not, instead of
    flickering partway through a sentence.
    """

    names: list[str] = field(default_factory=list)
    reject: int | None = None
    pre_delay_s: float = 0.0
    stall_s: float = 0.0
    stall_at: int | None = None
    truncate_chunk: int | None = None

    async def apply_pre_delay(self) -> None:
        if self.pre_delay_s > 0:
            await asyncio.sleep(self.pre_delay_s)

    def stall_for(self, chunk_index: int) -> Awaitable[None] | None:
        """Return a sleep to await if this chunk is the one that should stall."""

        if self.stall_at is not None and chunk_index == self.stall_at and self.stall_s > 0:
            return asyncio.sleep(self.stall_s)
        return None

    def truncate_at(self, chunk_index: int) -> bool:
        return self.truncate_chunk is not None and chunk_index >= self.truncate_chunk


class FaultInjector:
    """Holds the active fault set and draws a decision per request."""

    def __init__(self, specs: list[FaultSpec] | None = None, rng: random.Random | None = None) -> None:
        self._specs: list[FaultSpec] = list(specs or [])
        # Seedable so a fault run can be reproduced exactly from its report.
        self._rng = rng if rng is not None else random.Random()

    def replace(self, specs: list[FaultSpec]) -> None:
        self._specs = list(specs)

    def describe(self) -> list[str]:
        return [spec.raw for spec in self._specs]

    @property
    def active(self) -> bool:
        return bool(self._specs)

    def decide(self) -> FaultDecision:
        decision = FaultDecision()
        for spec in self._specs:
            if spec.probability < 1.0 and self._rng.random() >= spec.probability:
                continue
            decision.names.append(spec.raw)
            if spec.kind == "reject":
                decision.reject = spec.status
            elif spec.kind == "slow-start":
                decision.pre_delay_s += spec.sample_delay(self._rng)
            elif spec.kind == "stall":
                decision.stall_s = spec.sample_delay(self._rng)
                decision.stall_at = spec.at_chunk
            elif spec.kind == "truncate":
                decision.truncate_chunk = spec.at_chunk
        return decision


def parse_specs(values: list[str] | None) -> list[FaultSpec]:
    return [FaultSpec.parse(value) for value in (values or [])]
