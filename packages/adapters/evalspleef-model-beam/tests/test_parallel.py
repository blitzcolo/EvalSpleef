from __future__ import annotations

import pytest
from evalspleef_model_beam.errors import ParallelismMismatch
from evalspleef_model_beam.parallel import layout


@pytest.mark.parametrize("gpus", [1, 2, 4, 8])
def test_without_sizes_the_container_is_one_tensor_parallel_group(gpus: int) -> None:
    found = layout(gpus, ["--reasoning-parser", "qwen3"])
    assert (found.tp, found.pp, found.dp, found.explicit) == (gpus, 1, 1, False)


@pytest.mark.parametrize(
    ("gpus", "args", "expected"),
    [
        (4, ["--tp-size", "2", "--dp-size", "2"], (2, 1, 2)),
        (8, ["--tp-size=4", "--pp-size=2"], (4, 2, 1)),
        (8, ["--tensor-parallel-size", "8", "--dp-size", "4", "--enable-dp-attention"], (8, 1, 4)),
        (2, ["--pp-size", "2"], (1, 2, 1)),
    ],
)
def test_explicit_layouts_that_use_every_gpu(
    gpus: int, args: list[str], expected: tuple[int, int, int]
) -> None:
    found = layout(gpus, args)
    assert (found.tp, found.pp, found.dp, found.explicit) == (*expected, True)


@pytest.mark.parametrize(
    ("gpus", "args"),
    [
        (0, []),
        (9, []),
        (4, ["--tp-size", "2"]),
        (2, ["--tp-size", "2", "--dp-size", "2"]),
        (4, ["--tp-size", "4", "--dp-size", "3", "--enable-dp-attention"]),
        (4, ["--tp-size", "4", "--ep-size", "3"]),
        (2, ["--tp-size", "two"]),
        (2, ["--tp-size", "0"]),
    ],
)
def test_layouts_that_do_not_fit_are_refused(gpus: int, args: list[str]) -> None:
    with pytest.raises(ParallelismMismatch):
        layout(gpus, args)
