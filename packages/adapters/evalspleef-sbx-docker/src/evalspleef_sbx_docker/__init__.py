"""Docker sandbox provider for EvalSpleef.

Implements the sandbox SPI (TECH-DESIGN section 7.4.2) on top of the Docker
Engine API via ``aiodocker``. Ships the isolation profile and Linux probe set
(FR-SBX-11) and a conformance target for the local Docker daemon.
"""

from .provider import DockerProvider
from .sandbox import DockerSandbox

__all__ = ["DockerProvider", "DockerSandbox"]
