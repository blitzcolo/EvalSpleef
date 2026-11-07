"""SecretResolver implementations for the control plane.

All resolvers implement :class:`evalspleef.spi.secrets.SecretResolver`:
``resolve`` returns the value at the moment of use and implementations never
cache values to disk. Errors name the reference only, never the value.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from evalspleef.spi.secrets import SecretRef

from .secrets import SecretResolutionError, UnsupportedSchemeError

__all__ = ["CompositeResolver", "EnvResolver", "FileResolver", "VaultKv2Resolver"]


def _scheme_of(ref: str) -> str:
    return ref.split("://", 1)[0] if "://" in ref else ""


class EnvResolver:
    """Resolves ``env://NAME`` references against an environment mapping."""

    scheme = "env"

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ: Mapping[str, str] = os.environ if environ is None else environ

    def resolve(self, ref: SecretRef | str) -> str:
        name = str(ref)
        if not name.startswith("env://"):
            raise UnsupportedSchemeError(f"not an env:// reference: {name!r}")
        variable = name[len("env://") :]
        if not variable:
            raise SecretResolutionError(f"env reference names no variable: {name!r}")
        try:
            return self._environ[variable]
        except KeyError:
            raise SecretResolutionError(f"environment variable is not set: {variable!r}") from None


class FileResolver:
    """Resolves ``file:///abs/path`` references to the file's contents.

    A single trailing newline is stripped so ``echo value > secret`` files
    resolve to ``value``.
    """

    scheme = "file"

    def resolve(self, ref: SecretRef | str) -> str:
        name = str(ref)
        if not name.startswith("file://"):
            raise UnsupportedSchemeError(f"not a file:// reference: {name!r}")
        raw_path = name[len("file://") :]
        if not raw_path.startswith("/"):
            raise SecretResolutionError(
                f"file reference must be an absolute path (file:///...): {name!r}"
            )
        path = Path(raw_path)
        try:
            value = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SecretResolutionError(
                f"cannot read secret file {raw_path!r}: {exc.strerror or exc}"
            ) from exc
        return value[:-1] if value.endswith("\n") else value


class CompositeResolver:
    """Dispatches a reference to the first resolver accepting its scheme."""

    def __init__(self, resolvers: Sequence[Any]) -> None:
        if not resolvers:
            raise ValueError("CompositeResolver needs at least one resolver")
        self._resolvers = tuple(resolvers)

    def resolve(self, ref: SecretRef | str) -> str:
        name = str(ref)
        scheme = _scheme_of(name)
        for resolver in self._resolvers:
            resolver_scheme = getattr(resolver, "scheme", None)
            if resolver_scheme is None or resolver_scheme == scheme:
                try:
                    return str(resolver.resolve(name))
                except UnsupportedSchemeError:
                    continue
        raise UnsupportedSchemeError(
            f"no resolver accepts the scheme {scheme + '://' if scheme else ''!r} "
            f"of reference {name!r}"
        )


class VaultKv2Resolver:
    """Resolves ``vault://mount/path/key`` against Vault's HTTP KV v2 API.

    The referenced key is read from ``GET {base_url}/v1/{mount}/data/{path}``
    with the ``X-Vault-Token`` header. The Vault token itself comes from the
    constructor (an ``env://`` reference resolved through ``secrets`` keeps it
    out of configuration files); it is never included in error messages.

    Not verified against a real Vault server; tests inject an httpx transport.
    """

    scheme = "vault"

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.Client | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"Vault base_url must be http(s): {base_url!r}")
        self._base_url = base_url.rstrip("/")
        self._client = client if client is not None else httpx.Client(timeout=timeout_s)
        self._headers = {"X-Vault-Token": token}

    def resolve(self, ref: SecretRef | str) -> str:
        name = str(ref)
        if not name.startswith("vault://"):
            raise UnsupportedSchemeError(f"not a vault:// reference: {name!r}")
        parts = [part for part in name[len("vault://") :].split("/") if part]
        if len(parts) < 3:
            raise SecretResolutionError(
                f"vault reference must be vault://<mount>/<path>/<key>: {name!r}"
            )
        mount, path, key = parts[0], "/".join(parts[1:-1]), parts[-1]
        url = f"{self._base_url}/v1/{mount}/data/{path}"
        try:
            response = self._client.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise SecretResolutionError(f"Vault is unreachable at {url!r}: {exc}") from exc
        if response.status_code in (401, 403):
            raise SecretResolutionError(
                f"Vault denied access to {mount}/{path} (check the token's policy)"
            )
        if response.status_code == 404:
            raise SecretResolutionError(f"Vault has no secret at {mount}/{path}")
        if response.status_code != 200:
            raise SecretResolutionError(
                f"Vault returned HTTP {response.status_code} for {mount}/{path}"
            )
        payload: Any = response.json()
        try:
            data = payload["data"]["data"]
            value = data[key]
        except (KeyError, TypeError) as exc:
            raise SecretResolutionError(f"Vault secret {mount}/{path} has no key {key!r}") from exc
        if not isinstance(value, str):
            raise SecretResolutionError(
                f"Vault secret {mount}/{path} key {key!r} is not a string value"
            )
        return value
