from __future__ import annotations

import pytest
from evalspleef_model_novita.sizing import (
    GIB,
    ModelShape,
    choose_gpu,
    estimate_vram_gb,
    kv_cache_bytes,
    replicas_for,
)

from evalspleef.spi.errors import CapabilityNotSupported

# 8B-class shape: 32 layers x 8 kv heads x 128 head_dim x 2 (K,V) x 2 bytes = 128 KiB per token.
SHAPE = ModelShape(
    weight_bytes=16_000_000_000, num_hidden_layers=32, num_key_value_heads=8, head_dim=128
)


def test_kv_cache_is_128_kib_per_token_per_sequence() -> None:
    assert kv_cache_bytes(SHAPE, context_tokens=1, concurrency=1) == 128 * 2**10
    assert kv_cache_bytes(SHAPE, context_tokens=8192, concurrency=16) == 16 * GIB


def test_vram_is_weights_plus_kv_times_safety_factor() -> None:
    expected = (16_000_000_000 + 16 * GIB) * 1.2 / GIB
    assert estimate_vram_gb(SHAPE, 8192, 16) == pytest.approx(expected)
    assert expected == pytest.approx(37.08, abs=0.01)


@pytest.mark.parametrize(
    ("vram_gb", "name", "count"),
    [
        (20, "RTX 4090 24GB", 1),
        (28, "RTX 5090 32GB", 1),
        (37, "H100 SXM 80GB", 1),
        (100, "H200 141GB", 1),
        (200, "H200 141GB", 2),
    ],
)
def test_choose_gpu_takes_the_smallest_feasible_configuration(
    vram_gb: float, name: str, count: int
) -> None:
    choice = choose_gpu(vram_gb)
    assert (choice.sku.name, choice.count) == (name, count)


def test_choose_gpu_rejects_what_no_configuration_holds() -> None:
    with pytest.raises(CapabilityNotSupported):
        choose_gpu(2000)


@pytest.mark.parametrize(
    ("peak", "per_replica", "expected"), [(1, 16, 1), (16, 16, 1), (17, 16, 2), (40, 16, 3)]
)
def test_replicas_round_up(peak: int, per_replica: int, expected: int) -> None:
    assert replicas_for(peak, per_replica) == expected
