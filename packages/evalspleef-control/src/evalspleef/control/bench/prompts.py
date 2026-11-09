"""Seeded, deterministic benchmark prompts with a unique prefix per request.

The same (seed, index, length) always yields the same text, so a stored benchmark can be
re-sent exactly. Each prompt opens with a 64-bit tag drawn from (seed, index): requests never
share a prefix, so the single-request numbers carry no prefix-cache hit. Lengths are
approximate; the usage record reports the real input tokens.
"""

from __future__ import annotations

import random

__all__ = ["GENERATOR", "TOKENS_PER_LINE", "bench_prompt"]

GENERATOR = "ledger-v1"
TOKENS_PER_LINE = 31.5
"""Measured on a Qwen tokenizer (docs/evidence/beam-1m-kv.md); other tokenizers land near it."""

_WORDS = [
    "amber",
    "basil",
    "cedar",
    "delta",
    "ember",
    "fjord",
    "granite",
    "harbor",
    "indigo",
    "juniper",
    "kestrel",
    "lantern",
    "meadow",
    "nectar",
    "orchid",
    "pebble",
    "quartz",
    "raven",
    "saffron",
    "tundra",
    "umber",
    "violet",
    "willow",
    "xenon",
    "yarrow",
    "zephyr",
]


def bench_prompt(seed: int, index: int, approx_tokens: int) -> str:
    rng = random.Random(f"{GENERATOR}:{seed}:{index}")
    lines = [f"Ledger {index}-{rng.getrandbits(64):016x}."]
    count = max(1, int(approx_tokens / TOKENS_PER_LINE))
    lines += [
        f"Entry {number} of ledger {index}: {rng.choice(_WORDS)} {rng.choice(_WORDS)} weighs "
        f"{rng.randint(1, 99999)} units in bay {rng.randint(1, 999)}."
        for number in range(count)
    ]
    lines.append("Summarise the ledger in prose, entry by entry.")
    return "\n".join(lines)
