"""Console entry point (``spleef-gateway``): run both apps under uvicorn.

Configuration comes from an optional YAML file (``SPLEEF_GATEWAY_CONFIG``) overridden by
environment variables:

- ``SPLEEF_GATEWAY_ADMIN_TOKEN`` (required): bearer token for the admin plane;
- ``SPLEEF_GATEWAY_DATA_PORT`` (default 8390);
- ``SPLEEF_GATEWAY_ADMIN_PORT`` (default 8391);
- ``SPLEEF_GATEWAY_STORE`` (default ``memory://``): key/usage store URL.
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from typing import Any

import uvicorn
import yaml

from evalspleef.spi.secrets import SecretRef
from evalspleef.telemetry import configure_tracing, flush_tracing

from . import tracing
from .admin_app import create_admin_app
from .data_app import create_data_app
from .state import GatewayState
from .store import open_stores

ENV_PREFIX = "env://"


class _EnvSecretResolver:
    """Resolves ``env://NAME`` references against the process environment."""

    def resolve(self, ref: SecretRef | str) -> str:
        name = str(ref)
        if not name.startswith(ENV_PREFIX):
            raise ValueError(f"environment reference must start with {ENV_PREFIX!r}: {name!r}")
        variable = name[len(ENV_PREFIX) :]
        try:
            return os.environ[variable]
        except KeyError:
            raise KeyError(f"environment variable is not set: {variable!r}") from None


@dataclass(frozen=True)
class GatewayConfig:
    admin_token: str
    data_port: int
    admin_port: int
    store_url: str
    host: str = "127.0.0.1"


def load_config(environ: dict[str, str] | None = None) -> GatewayConfig:
    """Read config from an optional YAML file, then apply environment overrides."""
    env = os.environ if environ is None else environ
    file_values: dict[str, Any] = {}
    config_path = env.get("SPLEEF_GATEWAY_CONFIG")
    if config_path:
        with open(config_path, encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
        if isinstance(loaded, dict):
            file_values = loaded
    admin_token = env.get("SPLEEF_GATEWAY_ADMIN_TOKEN") or file_values.get("admin_token")
    if not admin_token:
        raise SystemExit("SPLEEF_GATEWAY_ADMIN_TOKEN is required")
    return GatewayConfig(
        admin_token=str(admin_token),
        data_port=int(env.get("SPLEEF_GATEWAY_DATA_PORT", file_values.get("data_port", 8390))),
        admin_port=int(env.get("SPLEEF_GATEWAY_ADMIN_PORT", file_values.get("admin_port", 8391))),
        store_url=str(env.get("SPLEEF_GATEWAY_STORE", file_values.get("store", "memory://"))),
        host=str(env.get("SPLEEF_GATEWAY_HOST", file_values.get("host", "127.0.0.1"))),
    )


class _GatewayServer(uvicorn.Server):
    """Signal handling is done once for both apps in :func:`serve`."""

    def install_signal_handlers(self) -> None:
        pass


async def serve(config: GatewayConfig) -> None:
    """Start the data and admin apps on ``config`` and run until interrupted."""
    # A no-op unless an OTLP endpoint is configured; spans flush again at shutdown.
    configure_tracing(tracing.SERVICE_NAME)
    key_store, usage_store = await open_stores(config.store_url)
    state = GatewayState(
        key_store=key_store,
        usage_store=usage_store,
        secrets=_EnvSecretResolver(),
    )
    data_app = create_data_app(state)
    admin_app = create_admin_app(state, config.admin_token)
    data_server = _GatewayServer(
        uvicorn.Config(data_app, host=config.host, port=config.data_port, log_level="info")
    )
    admin_server = _GatewayServer(
        uvicorn.Config(admin_app, host=config.host, port=config.admin_port, log_level="info")
    )

    def _shutdown() -> None:
        data_server.should_exit = True
        admin_server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    try:
        await asyncio.gather(data_server.serve(), admin_server.serve())
    finally:
        await state.close()
        # The process exits right after shutdown: export the batched spans now.
        flush_tracing()


def main() -> None:
    asyncio.run(serve(load_config()))


if __name__ == "__main__":
    main()
