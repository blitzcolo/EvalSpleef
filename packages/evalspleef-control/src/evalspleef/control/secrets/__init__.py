"""Secret resolution for the control plane.

Configuration files hold ``SecretRef`` strings; these resolvers turn a
reference into its value at the moment of use. Supported schemes:

- ``env://NAME`` — an environment variable,
- ``file:///abs/path`` — the contents of a file (trailing newline stripped),
- ``vault://mount/path/key`` — a key inside a HashiCorp Vault KV v2 secret.

Resolvers never log values and never include them in exceptions; error
messages name only the reference.
"""

from .resolvers import CompositeResolver, EnvResolver, FileResolver, VaultKv2Resolver
from .secrets import SecretResolutionError, UnsupportedSchemeError

__all__ = [
    "CompositeResolver",
    "EnvResolver",
    "FileResolver",
    "SecretResolutionError",
    "UnsupportedSchemeError",
    "VaultKv2Resolver",
]
