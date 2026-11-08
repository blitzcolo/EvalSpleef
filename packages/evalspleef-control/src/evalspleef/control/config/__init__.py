"""Configuration file loading for the control plane.

Pydantic v2 models and loaders for ``models.yaml``, ``providers.yaml`` and
``batch.yaml`` exactly as specified in TECH-DESIGN section 14. Model entries
convert to the immutable SPI dataclasses; secret values are referenced by
``SecretRef`` strings and never loaded here.

Every validation error names the file and the dotted path of the offending
field (:class:`ConfigError`).
"""

from .batch import BatchFileConfig, EndpointPolicy, load_batch
from .errors import ConfigError
from .models import (
    HFSourceConfig,
    ModelConfig,
    VendorSourceConfig,
    load_hf_token_expiry,
    load_models,
)
from .providers import (
    EgressProxyConfig,
    GatewayConfig,
    ModelAccountConfig,
    ProvidersFileConfig,
    RouteEntry,
    SandboxInstanceConfig,
    load_providers,
)

__all__ = [
    "BatchFileConfig",
    "ConfigError",
    "EgressProxyConfig",
    "EndpointPolicy",
    "GatewayConfig",
    "HFSourceConfig",
    "ModelAccountConfig",
    "ModelConfig",
    "ProvidersFileConfig",
    "RouteEntry",
    "SandboxInstanceConfig",
    "VendorSourceConfig",
    "load_batch",
    "load_hf_token_expiry",
    "load_models",
    "load_providers",
]
