from __future__ import annotations

import pytest
from evalspleef_model_vpc_vllm.clock import SteppingClock
from evalspleef_model_vpc_vllm.k8s import InMemoryKubeApi

from evalspleef.testing.secrets import DictSecretResolver

API_KEY = "sk-vpc-vllm-test"
API_KEY_REF = "vault://vpc-vllm/test"
HF_TOKEN_REF = "vault://hf/test"
HF_SECRET = "hf_token_secret_do_not_leak"


@pytest.fixture
def clock() -> SteppingClock:
    return SteppingClock()


@pytest.fixture
def secrets() -> DictSecretResolver:
    return DictSecretResolver({API_KEY_REF: API_KEY, HF_TOKEN_REF: HF_SECRET})


@pytest.fixture
def kube_api() -> InMemoryKubeApi:
    return InMemoryKubeApi()
