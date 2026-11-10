"""Billing export parsing: the Novita billing API JSON maps onto vendor-free bill lines."""

from __future__ import annotations

import json

import pytest
from evalspleef_model_novita.dedicated import NovitaDedicatedProvider

from evalspleef.spi.errors import CapabilityNotSupported
from evalspleef.spi.model import (
    DeploymentPlan,
    EndpointHandle,
    EndpointState,
    ManagementMode,
    ModelEndpointProvider,
    ModelSpec,
    TeardownMode,
    UpstreamRoute,
    ValidationReport,
)
from evalspleef.spi.secrets import SecretResolver


def _provider(secrets: SecretResolver) -> NovitaDedicatedProvider:
    return NovitaDedicatedProvider(account="acme", config={}, secrets=secrets)


def test_parse_bill_maps_product_categories(secrets: SecretResolver) -> None:
    payload = {
        "items": [
            {
                "transaction_id": "t-1",
                "product_category": "llm_dedicated_endpoint",
                "amount": 40.0,
                "quantity": 20.0,
                "unit": "gpu·h",
            },
            {
                "transaction_id": "t-2",
                "product_category": "llm",
                "amount": 1.0,
                "quantity": 1.0,
                "unit": "mtok",
            },
            {
                "transaction_id": "t-3",
                "product_category": "sandbox",
                "amount": 0.12,
                "quantity": 7200.0,
                "unit": "vcpu·s",
            },
            {"transaction_id": "t-4", "product_category": "storage", "amount": 0.05},
            {"transaction_id": "t-5", "product_category": "some_future_product", "amount": 99.0},
        ]
    }
    lines = _provider(secrets).parse_bill(json.dumps(payload))
    # The unknown future category is skipped, not misclassified.
    assert [(line.kind, line.amount_usd) for line in lines] == [
        ("gpu", 40.0),
        ("tokens", 1.0),
        ("sandbox", 0.12),
        ("infra", 0.05),
    ]
    assert [line.ref for line in lines] == ["t-1", "t-2", "t-3", "t-4"]
    assert lines[0].quantity == 20.0 and lines[0].unit == "gpu·h"


def test_parse_bill_accepts_camel_case_and_bare_list(secrets: SecretResolver) -> None:
    provider = _provider(secrets)
    camel = provider.parse_bill(
        json.dumps({"transactions": [{"id": "x-1", "productCategory": "llm", "amount": 3.0}]})
    )
    assert [(line.kind, line.ref) for line in camel] == [("tokens", "x-1")]
    from_list = provider.parse_bill(
        json.dumps([{"id": "x-2", "product_category": "network", "amount": 0.01}])
    )
    assert [line.kind for line in from_list] == ["infra"]
    with pytest.raises(ValueError, match="no items"):
        provider.parse_bill(json.dumps({"nope": []}))


def test_provider_declares_billing_support(secrets: SecretResolver) -> None:
    assert _provider(secrets).supports_billing is True


class _BareProvider(ModelEndpointProvider):
    """Minimal concrete provider exercising the optional-billing default."""

    name = "bare"
    management_mode = ManagementMode.EXTERNAL

    async def validate(self, spec: ModelSpec) -> ValidationReport:
        return ValidationReport(ok=True)

    async def plan(self, spec: ModelSpec) -> DeploymentPlan:
        return DeploymentPlan(replicas_min=0, replicas_max=0)

    async def deploy(self, spec: ModelSpec, plan: DeploymentPlan) -> EndpointHandle:
        raise NotImplementedError

    async def status(self, handle: EndpointHandle) -> EndpointState:
        return EndpointState.RETIRED

    async def wake(self, handle: EndpointHandle, timeout_s: int) -> EndpointState:
        return EndpointState.RETIRED

    async def teardown(self, handle: EndpointHandle, mode: TeardownMode) -> None:
        return None

    def upstream(self, handle: EndpointHandle) -> UpstreamRoute:
        raise NotImplementedError


def test_default_provider_rejects_parse_bill(secrets: SecretResolver) -> None:
    provider = _BareProvider(account="acme", config={}, secrets=secrets)
    assert provider.supports_billing is False
    with pytest.raises(CapabilityNotSupported, match="parse_bill"):
        provider.parse_bill("{}")
