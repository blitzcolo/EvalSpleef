"""The parallel layout of an SGLang launch on one Beam container.

People choose the layout (a preset's engine arguments); this module only checks that it fits the
GPU count, so a wrong layout is refused before a GPU is billed instead of failing inside the
engine. Without explicit sizes the whole container is one tensor-parallel group.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .errors import ParallelismMismatch

MAX_GPUS = 8
"""The largest GPU count Beam offers in one container."""

_FLAGS = {
    "tp": ("--tp-size", "--tensor-parallel-size"),
    "pp": ("--pp-size", "--pipeline-parallel-size"),
    "dp": ("--dp-size", "--data-parallel-size"),
    "ep": ("--ep-size", "--expert-parallel-size"),
}
_DP_ATTENTION = "--enable-dp-attention"


@dataclass(frozen=True)
class Layout:
    tp: int
    pp: int
    dp: int
    ep: int
    dp_attention: bool
    explicit: bool
    """True when the engine arguments set any size; the adapter then adds none."""


def _sizes(args: Sequence[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for index, arg in enumerate(args):
        name, _, inline = arg.partition("=")
        for key, flags in _FLAGS.items():
            if name not in flags:
                continue
            raw = inline if inline else (args[index + 1] if index + 1 < len(args) else "")
            try:
                found[key] = int(raw)
            except ValueError:
                raise ParallelismMismatch(
                    f"{name} needs an integer, got {raw!r}", detail={"flag": name}
                ) from None
            if found[key] < 1:
                raise ParallelismMismatch(f"{name} must be at least 1", detail={"flag": name})
    return found


def layout(gpu_count: int, args: Sequence[str]) -> Layout:
    """The layout the engine arguments describe on ``gpu_count`` GPUs, or ParallelismMismatch."""
    if not 1 <= gpu_count <= MAX_GPUS:
        raise ParallelismMismatch(
            f"gpu_count {gpu_count} is outside 1..{MAX_GPUS}", detail={"gpu_count": gpu_count}
        )
    sizes = _sizes(args)
    dp_attention = _DP_ATTENTION in args
    explicit = any(key in sizes for key in ("tp", "pp", "dp"))
    tp = sizes.get("tp", 1 if explicit else gpu_count)
    pp = sizes.get("pp", 1)
    dp = sizes.get("dp", 1)
    ep = sizes.get("ep", 1)
    detail = {"gpu_count": gpu_count, "tp": tp, "pp": pp, "dp": dp, "ep": ep}
    if dp_attention:
        # DP attention splits the tensor-parallel group; it adds no GPUs of its own.
        if tp * pp != gpu_count or tp % dp:
            raise ParallelismMismatch(
                f"with {_DP_ATTENTION}, tp x pp must equal {gpu_count} and dp must divide tp",
                detail=detail,
            )
    elif tp * pp * dp != gpu_count:
        raise ParallelismMismatch(
            f"tp x pp x dp = {tp * pp * dp} does not use the {gpu_count} GPUs", detail=detail
        )
    if tp % ep:
        raise ParallelismMismatch("ep must divide tp", detail=detail)
    return Layout(tp=tp, pp=pp, dp=dp, ep=ep, dp_attention=dp_attention, explicit=explicit)
