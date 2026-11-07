"""Hugging Face access checking for the control plane (TECH-DESIGN section 6.5).

:class:`HuggingFaceChecker` implements the SPI ``HFAccessChecker`` against an
injected, ``HfApi``-like client so tests never touch the Hub. The real
client wraps ``huggingface_hub``; mirror copying is implemented as
snapshot-download + folder-upload of the pinned revision only.

Not verified against the real Hub: all tests use a fake client with canned
responses.
"""

from .checker import SMALL_FILES, HFAccessError, HuggingFaceChecker, default_mirror_name
from .client import HfHubClient, HubClient, HubRepoInfo
from .tokens import TokenExpiryReport, check_token_expiry

__all__ = [
    "SMALL_FILES",
    "HFAccessError",
    "HfHubClient",
    "HubClient",
    "HubRepoInfo",
    "HuggingFaceChecker",
    "TokenExpiryReport",
    "check_token_expiry",
    "default_mirror_name",
]
