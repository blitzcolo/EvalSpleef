"""Keep the engine log of a paid endpoint at teardown, without credentials in it.

The log is fetched before the provider tears the endpoint down, redacted of every secret value
the endpoint was configured with (AGENTS.md rule 5) and written under the state directory. The
outcome is returned as a plain mapping for the retire event payload. A failed fetch is recorded
there, never raised: teardown stops paid compute and must not wait on a log.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evalspleef.spi.errors import CapabilityNotSupported, SpleefError
from evalspleef.spi.model import EndpointHandle, HFSource, ModelEndpointProvider
from evalspleef.spi.secrets import SecretResolver

__all__ = ["DEFAULT_MAX_LOG_BYTES", "EngineLogStore", "redact"]

DEFAULT_MAX_LOG_BYTES = 8 * 1024 * 1024
_MIN_SECRET_CHARS = 8
"""Shorter values are not redacted: they would also match ordinary log words."""
_MAX_SAME_SECOND = 100
_REF = re.compile(r"^(env|file|vault)://")


def redact(text: str, values: Iterable[str]) -> tuple[str, int]:
    """``text`` with every secret value replaced by ``***`` and the number of replacements."""
    count = 0
    for value in sorted(set(values), key=len, reverse=True):
        count += text.count(value)
        text = text.replace(value, "***")
    return text, count


def _refs(handle: EndpointHandle, provider: ModelEndpointProvider) -> set[str]:
    """Every secret reference the endpoint's deployment can have resolved."""
    refs = {handle.credential_ref}
    if isinstance(handle.model.source, HFSource) and handle.model.source.token_ref:
        refs.add(handle.model.source.token_ref)
    for mapping in (handle.model.deployment.params, provider.config):
        refs.update(v for v in mapping.values() if isinstance(v, str) and _REF.match(v))
    return {ref for ref in refs if _REF.match(ref)}


@dataclass(frozen=True)
class EngineLogStore:
    """Where engine logs go (``<root>/<endpoint_id>/<utc>.log``) and which secrets to strip."""

    root: Path
    secrets: SecretResolver
    max_bytes: int = DEFAULT_MAX_LOG_BYTES

    def _secret_values(self, handle: EndpointHandle, provider: ModelEndpointProvider) -> set[str]:
        values: set[str] = set()
        for ref in _refs(handle, provider):
            try:
                value = self.secrets.resolve(ref)
            except (KeyError, ValueError, OSError):
                continue  # a reference that does not resolve cannot have leaked a value
            if len(value) >= _MIN_SECRET_CHARS:
                values.add(value)
        return values

    def _write(self, endpoint_id: str, stamp: str, data: bytes) -> Path:
        directory = self.root / endpoint_id
        directory.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^0-9TZ]", "", stamp)
        for attempt in range(_MAX_SAME_SECOND):
            path = directory / (f"{name}.log" if attempt == 0 else f"{name}-{attempt}.log")
            try:
                # Created private from the first byte: a log may hold what redaction missed.
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            return path
        raise FileExistsError(f"too many engine logs of {endpoint_id} in the same second")

    async def capture(
        self, provider: ModelEndpointProvider, handle: EndpointHandle, stamp: str
    ) -> Mapping[str, Any]:
        """Fetch, redact and store the log; the result is the ``engine_log`` event field."""
        try:
            log = await provider.engine_log(handle, max_bytes=self.max_bytes)
        except CapabilityNotSupported:
            return {"status": "unsupported"}
        except Exception as exc:  # recorded in the retire event; teardown must still run
            reason = exc.message if isinstance(exc, SpleefError) else str(exc)
            values = self._secret_values(handle, provider)
            return {
                "status": "error",
                "error": redact(f"{type(exc).__name__}: {reason}", values)[0],
            }
        values = self._secret_values(handle, provider)
        text, redactions = redact(log.text, values)
        data = text.encode()
        try:
            path = await asyncio.to_thread(self._write, handle.endpoint_id, stamp, data)
        except OSError as exc:
            return {"status": "error", "error": f"cannot store the engine log: {exc.strerror}"}
        return {
            "status": "stored",
            "path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "truncated": log.truncated,
            "dropped": log.dropped,
            "redactions": redactions,
        }
