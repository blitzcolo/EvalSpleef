"""VRAM estimate and GPU choice for the dedicated endpoint plan (TECH-DESIGN section 6.6.2).

Pure functions: no clock, no I/O, so a plan is reproducible from its inputs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from evalspleef.spi.errors import CapabilityNotSupported

GIB = 1024**3
SAFETY_FACTOR = 1.2


@dataclass(frozen=True)
class GpuSku:
    name: str
    vram_gb: int
    price_per_hour_usd: float | None
    """``None`` when the PRD price table has no figure; the operator reads it off the console."""


# Prices: PRD section 8.1 (product page). The RTX 5090 has no published figure there.
GPU_CATALOG: tuple[GpuSku, ...] = (
    GpuSku("RTX 4090 24GB", 24, 0.61),
    GpuSku("RTX 5090 32GB", 32, None),
    GpuSku("H100 SXM 80GB", 80, 1.99),
    GpuSku("H200 141GB", 141, 2.99),
)
_GPU_COUNTS = (1, 2, 4, 8)


@dataclass(frozen=True)
class ModelShape:
    """The architecture numbers the KV cache term needs (``config.json`` field names)."""

    weight_bytes: int
    num_hidden_layers: int
    num_key_value_heads: int
    head_dim: int
    kv_dtype_bytes: int = 2


def kv_cache_bytes(shape: ModelShape, context_tokens: int, concurrency: int) -> int:
    """context x layers x kv heads x head_dim x 2 (K and V) x dtype bytes x concurrency."""
    return (
        context_tokens
        * shape.num_hidden_layers
        * shape.num_key_value_heads
        * shape.head_dim
        * 2
        * shape.kv_dtype_bytes
        * concurrency
    )


def estimate_vram_gb(shape: ModelShape, context_tokens: int, concurrency: int) -> float:
    """(weights + KV cache) x 1.2, in GiB."""
    total = shape.weight_bytes + kv_cache_bytes(shape, context_tokens, concurrency)
    return total * SAFETY_FACTOR / GIB


@dataclass(frozen=True)
class GpuChoice:
    sku: GpuSku
    count: int

    @property
    def total_vram_gb(self) -> int:
        return self.sku.vram_gb * self.count


def choose_gpu(
    vram_gb: float,
    catalog: tuple[GpuSku, ...] = GPU_CATALOG,
) -> GpuChoice:
    """Smallest feasible configuration: fewest GPUs first, then least VRAM per GPU."""
    ordered = sorted(catalog, key=lambda sku: sku.vram_gb)
    for count in _GPU_COUNTS:
        for sku in ordered:
            if sku.vram_gb * count >= vram_gb:
                return GpuChoice(sku, count)
    largest = ordered[-1]
    raise CapabilityNotSupported(
        f"no catalog GPU configuration holds {vram_gb:.1f} GiB "
        f"(largest: {_GPU_COUNTS[-1]} x {largest.name})"
    )


def replicas_for(peak_concurrency: int, max_concurrency_per_replica: int) -> int:
    return max(1, math.ceil(peak_concurrency / max_concurrency_per_replica))


def apply_price_overrides(
    catalog: tuple[GpuSku, ...], overrides: Mapping[str, float]
) -> tuple[GpuSku, ...]:
    """Replace prices by SKU name, for example after a console price change."""
    return tuple(
        GpuSku(sku.name, sku.vram_gb, overrides[sku.name]) if sku.name in overrides else sku
        for sku in catalog
    )
