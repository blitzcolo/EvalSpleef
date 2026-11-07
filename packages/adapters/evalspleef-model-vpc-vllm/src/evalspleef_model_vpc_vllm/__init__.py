"""Model provider: self-hosted vLLM/SGLang on Kubernetes or GPU instances."""

from .provider import VpcVllmProvider

__all__ = ["VpcVllmProvider"]
