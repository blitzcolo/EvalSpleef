"""Provider registry: entry-point discovery and provider instantiation (FR-ABS-02).

Adapters are discovered exclusively through ``importlib.metadata`` entry
points (TECH-DESIGN section 4); this package never imports an adapter module
statically. One adapter can back several configured instances (for example
two Novita accounts or three Mac minis). ``capabilities()`` and
``isolation_profile()`` are cached per instance because they are declared,
stable values.
"""

from .registry import (
    MODEL_PROVIDER_GROUP,
    RUNNER_GROUP,
    SANDBOX_PROVIDER_GROUP,
    ProviderNotFoundError,
    ProviderRegistry,
    RouteError,
    RoutePreference,
)

__all__ = [
    "MODEL_PROVIDER_GROUP",
    "RUNNER_GROUP",
    "SANDBOX_PROVIDER_GROUP",
    "ProviderNotFoundError",
    "ProviderRegistry",
    "RouteError",
    "RoutePreference",
]
