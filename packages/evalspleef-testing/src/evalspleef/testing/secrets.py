"""Secret resolver doubles: in-memory dictionaries and environment variables.

Both resolve at the moment of use and keep nothing on disk, as the SPI requires.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from evalspleef.spi.secrets import SecretRef

ENV_PREFIX = "env://"


class DictSecretResolver:
    """Resolves references that are literal keys of an in-memory mapping."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values: Mapping[str, str] = values if values is not None else {}

    def resolve(self, ref: SecretRef | str) -> str:
        name = str(ref)
        try:
            return self._values[name]
        except KeyError:
            raise KeyError(f"secret not found: {name!r}") from None


class EnvSecretResolver:
    """Resolves ``env://NAME`` references against an environment mapping."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ: Mapping[str, str] = os.environ if environ is None else environ

    def resolve(self, ref: SecretRef | str) -> str:
        name = str(ref)
        if not name.startswith(ENV_PREFIX):
            raise ValueError(f"environment reference must start with {ENV_PREFIX!r}: {name!r}")
        variable = name[len(ENV_PREFIX) :]
        if not variable:
            raise ValueError(f"environment reference names no variable: {name!r}")
        try:
            return self._environ[variable]
        except KeyError:
            raise KeyError(f"environment variable is not set: {variable!r}") from None
