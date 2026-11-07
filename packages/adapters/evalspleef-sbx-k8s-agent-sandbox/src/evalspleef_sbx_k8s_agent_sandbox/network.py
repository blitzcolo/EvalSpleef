"""Cilium egress policy generation for one sandbox.

The agent-sandbox provider cannot restrict egress with a stock Kubernetes
NetworkPolicy, because hostname allowlists need DNS-aware enforcement. Every
sandbox therefore gets a ``CiliumNetworkPolicy`` in the ``cilium.io/v2`` group:

* ``off`` -> an empty egress list, which Cilium turns into default-deny egress;
* ``docs_whitelist`` / ``package_mirror`` -> DNS to kube-dns plus ``toFQDNs``
  rules for every allowlisted hostname on ports 80 and 443;
* ``full`` -> no policy at all (the platform logs egress outside the sandbox).

The policy selects the sandbox pod by the labels copied from the SPI spec, so
the manifest is generated before the Sandbox object is created.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from evalspleef.spi.caps import NetMode
from evalspleef.spi.sandbox import NetworkPolicy

CILIUM_GROUP = "cilium.io"
CILIUM_VERSION = "v2"
CILIUM_PLURAL = "ciliumnetworkpolicies"

_DNS_PORTS = [
    {"port": "53", "protocol": "UDP"},
    {"port": "53", "protocol": "TCP"},
]
_HTTP_PORTS = [
    {"port": "80", "protocol": "TCP"},
    {"port": "443", "protocol": "TCP"},
]


def policy_name(sandbox_name: str) -> str:
    """Name of the egress policy owned by one sandbox."""
    return f"{sandbox_name}-egress"


def build_egress_policy(
    *,
    sandbox_name: str,
    namespace: str,
    pod_labels: Mapping[str, str],
    policy: NetworkPolicy,
) -> dict[str, Any] | None:
    """The ``CiliumNetworkPolicy`` body for ``policy``, or ``None`` for full access."""
    if policy.mode is NetMode.FULL:
        return None
    egress: list[dict[str, Any]] = []
    if policy.mode in (NetMode.DOCS_WHITELIST, NetMode.PACKAGE_MIRROR):
        # Cilium needs a visible DNS rule to program toFQDNs rules.
        egress.append(
            {
                "toEndpoints": [
                    {
                        "matchLabels": {
                            "k8s:io.kubernetes.pod.namespace": "kube-system",
                            "k8s:k8s-app": "kube-dns",
                        }
                    }
                ],
                "toPorts": [{"ports": _DNS_PORTS}],
            }
        )
        for host in policy.allow_hosts:
            egress.append(
                {
                    "toFQDNs": [{"matchName": host}],
                    "toPorts": [{"ports": _HTTP_PORTS}],
                }
            )
    return {
        "apiVersion": f"{CILIUM_GROUP}/{CILIUM_VERSION}",
        "kind": "CiliumNetworkPolicy",
        "metadata": {
            "name": policy_name(sandbox_name),
            "namespace": namespace,
            "labels": dict(pod_labels),
            "annotations": {"spleef.io/network-mode": policy.mode.value},
        },
        "spec": {
            "endpointSelector": {"matchLabels": dict(pod_labels)},
            "egress": egress,
        },
    }
