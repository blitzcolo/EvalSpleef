"""What a benchmark measures: request shapes, repeats and the concurrency sweep."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from evalspleef.core.perf import MIN_GAIN, TRIM_FRACTION

__all__ = ["BenchPlan", "Shape", "parse_shape"]


@dataclass(frozen=True)
class Shape:
    """A declared (input length, output length) pair, in tokens."""

    input_tokens: int
    output_tokens: int

    def label(self) -> str:
        return f"{self.input_tokens}x{self.output_tokens}"


def parse_shape(text: str) -> Shape:
    """``2048x128`` -> Shape(2048, 128)."""
    head, sep, tail = text.lower().partition("x")
    if not sep or not head.strip().isdigit() or not tail.strip().isdigit():
        raise ValueError(f"a shape is INPUTxOUTPUT tokens, for example 2048x128: {text!r}")
    shape = Shape(int(head), int(tail))
    if shape.input_tokens < 1 or shape.output_tokens < 2:
        raise ValueError(f"a shape needs at least 1 input and 2 output tokens: {text!r}")
    return shape


@dataclass(frozen=True)
class BenchPlan:
    """The single-request phase, then a closed-loop concurrency sweep per sweep shape.

    Single requests run one at a time with unique prompt prefixes, so no request hits a prefix
    cache. Each sweep level runs ``concurrency`` workers that send ``requests_per_worker``
    requests back to back; the sweep stops at the first level that fails or gains less than
    ``min_gain`` system decode throughput over the previous one.
    """

    shapes: tuple[Shape, ...]
    seed: int
    single_repeats: int = 3
    sweep_shapes: tuple[Shape, ...] = ()
    concurrency: tuple[int, ...] = (1, 2, 4, 8, 16)
    requests_per_worker: int = 4
    trim: float = TRIM_FRACTION
    min_gain: float = MIN_GAIN
    request_timeout_s: float = 600.0
    ignore_eos: bool = False
    """Ask the engine to ignore end-of-sequence so every request reaches its output length
    (engines that support it; hosted APIs may reject or ignore the field)."""
    extra_body: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.shapes:
            raise ValueError("a benchmark needs at least one shape")
        if self.single_repeats < 1 or self.requests_per_worker < 1:
            raise ValueError("repeats and requests per worker must be at least 1")
        if not self.concurrency or any(level < 1 for level in self.concurrency):
            raise ValueError("concurrency levels must be positive")
        if list(self.concurrency) != sorted(set(self.concurrency)):
            raise ValueError("concurrency levels must rise strictly")

    @property
    def sweep(self) -> tuple[Shape, ...]:
        """The shapes swept; the first declared shape when none is named."""
        return self.sweep_shapes or self.shapes[:1]

    def request_count(self) -> int:
        """Upper bound on requests sent (the sweep may stop early)."""
        single = len(self.shapes) * self.single_repeats
        sweep = len(self.sweep) * sum(self.concurrency) * self.requests_per_worker
        return single + sweep

    def token_budget(self) -> int:
        """Upper bound on tokens the plan can spend, with headroom for tokenizer drift."""
        single = sum((s.input_tokens + s.output_tokens) * self.single_repeats for s in self.shapes)
        per_sweep = sum(self.concurrency) * self.requests_per_worker
        sweep = sum((s.input_tokens + s.output_tokens) * per_sweep for s in self.sweep)
        return int((single + sweep) * 1.5) + 1000

    def to_dict(self) -> dict[str, Any]:
        return {
            "shapes": [shape.label() for shape in self.shapes],
            "seed": self.seed,
            "single_repeats": self.single_repeats,
            "sweep_shapes": [shape.label() for shape in self.sweep],
            "concurrency": list(self.concurrency),
            "requests_per_worker": self.requests_per_worker,
            "trim": self.trim,
            "min_gain": self.min_gain,
            "request_timeout_s": self.request_timeout_s,
            "ignore_eos": self.ignore_eos,
            "extra_body": dict(self.extra_body),
        }
