"""apple/container sandbox provider for EvalSpleef.

Implements the sandbox SPI (TECH-DESIGN section 7.4.4) on top of the macOS 26
``container`` CLI. Every container is its own lightweight Linux VM (isolation
L3). Ships the isolation profile, the Linux probe set (FR-SBX-11) and a
fake-backed conformance target.
"""

from .provider import AppleContainerProvider
from .sandbox import AppleContainerSandbox

__all__ = ["AppleContainerProvider", "AppleContainerSandbox"]
