"""Conversions between SPI value types and their protobuf messages."""

from __future__ import annotations

import dataclasses
import enum
import json
from pathlib import Path
from typing import Any, cast

from evalspleef.spi.caps import AllowKind, Arch, Isolation, NetMode, OSFamily
from evalspleef.spi.isolation import InvariantEntry, IsolationProfile
from evalspleef.spi.sandbox import (
    EnforcedLimit,
    GuiSupport,
    NetworkPolicy,
    Resources,
    SandboxCapabilities,
    SandboxRole,
    SandboxSpec,
    SnapshotSupport,
    TemplateKind,
    TemplateRef,
)

from ._pb import hostd_pb2 as pb


def _jsonable(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set | frozenset):
        return sorted(value, key=str)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def to_json(document: object) -> str:
    """Provider documents are plain data; exotic values degrade to their text form."""
    return json.dumps(document, default=_jsonable, sort_keys=True)


def from_json(text: str) -> Any:
    return json.loads(text)


# -- spec ---------------------------------------------------------------------


def _resources_to_pb(value: Resources) -> pb.Resources:
    message = pb.Resources(
        cpus=value.cpus, memory_mb=value.memory_mb, disk_mb=value.disk_mb, gpus=value.gpus
    )
    if value.gpu_type is not None:
        message.gpu_type = value.gpu_type
    return message


def _resources_from_pb(message: pb.Resources) -> Resources:
    return Resources(
        cpus=message.cpus,
        memory_mb=message.memory_mb,
        disk_mb=message.disk_mb,
        gpus=message.gpus,
        gpu_type=message.gpu_type if message.HasField("gpu_type") else None,
    )


def network_to_pb(policy: NetworkPolicy) -> pb.NetworkPolicy:
    message = pb.NetworkPolicy(
        mode=policy.mode.value,
        allow_hosts=policy.allow_hosts,
        allow_cidrs=policy.allow_cidrs,
    )
    if policy.via_proxy is not None:
        message.via_proxy = policy.via_proxy
    return message


def network_from_pb(message: pb.NetworkPolicy) -> NetworkPolicy:
    return NetworkPolicy(
        mode=NetMode(message.mode),
        allow_hosts=tuple(message.allow_hosts),
        allow_cidrs=tuple(message.allow_cidrs),
        via_proxy=message.via_proxy if message.HasField("via_proxy") else None,
    )


def spec_to_pb(spec: SandboxSpec) -> pb.SandboxSpec:
    return pb.SandboxSpec(
        trial_id=spec.trial_id,
        lease_id=spec.lease_id,
        role=spec.role,
        os=spec.os.value,
        arch=spec.arch.value,
        template=pb.TemplateRef(
            kind=spec.template.kind, ref=spec.template.ref, digest=spec.template.digest
        ),
        resources=_resources_to_pb(spec.resources),
        network=network_to_pb(spec.network),
        run_as_user=spec.run_as_user,
        env=dict(spec.env),
        labels=dict(spec.labels),
        max_lifetime_s=spec.max_lifetime_s,
        gui=spec.gui,
    )


def spec_from_pb(message: pb.SandboxSpec) -> SandboxSpec:
    return SandboxSpec(
        trial_id=message.trial_id,
        lease_id=message.lease_id,
        role=cast(SandboxRole, message.role),
        os=OSFamily(message.os),
        arch=Arch(message.arch),
        template=TemplateRef(
            kind=cast(TemplateKind, message.template.kind),
            ref=message.template.ref,
            digest=message.template.digest,
        ),
        resources=_resources_from_pb(message.resources),
        network=network_from_pb(message.network),
        run_as_user=message.run_as_user,
        env=dict(message.env),
        labels=dict(message.labels),
        max_lifetime_s=message.max_lifetime_s,
        gui=message.gui,
    )


# -- capabilities and isolation profile ------------------------------------------


def capabilities_to_pb(caps: SandboxCapabilities) -> pb.SandboxCapabilities:
    message = pb.SandboxCapabilities(
        os_families=sorted(item.value for item in caps.os_families),
        arches=sorted(item.value for item in caps.arches),
        isolation=int(caps.isolation),
        net_modes=sorted(item.value for item in caps.net_modes),
        allowlist_kinds=sorted(caps.allowlist_kinds),
        dynamic_network_policy=caps.dynamic_network_policy,
        streaming=caps.streaming,
        stdin=caps.stdin,
        snapshot=caps.snapshot,
        snapshot_fanout=caps.snapshot_fanout,
        pause_resume=caps.pause_resume,
        gui=caps.gui,
        gpu=caps.gpu,
        port_expose=caps.port_expose,
        host_mounts=caps.host_mounts,
        enforces_limits=sorted(caps.enforces_limits),
        typical_start_s=caps.typical_start_s,
        reset_cost_s=caps.reset_cost_s,
    )
    if caps.max_concurrency is not None:
        message.max_concurrency = caps.max_concurrency
    if caps.max_lifetime_s is not None:
        message.max_lifetime_s = caps.max_lifetime_s
    if caps.max_resources is not None:
        message.max_resources.CopyFrom(_resources_to_pb(caps.max_resources))
    return message


def capabilities_from_pb(message: pb.SandboxCapabilities) -> SandboxCapabilities:
    return SandboxCapabilities(
        os_families=frozenset(OSFamily(item) for item in message.os_families),
        arches=frozenset(Arch(item) for item in message.arches),
        isolation=Isolation(message.isolation),
        net_modes=frozenset(NetMode(item) for item in message.net_modes),
        allowlist_kinds=frozenset(cast(AllowKind, item) for item in message.allowlist_kinds),
        dynamic_network_policy=message.dynamic_network_policy,
        streaming=message.streaming,
        stdin=message.stdin,
        snapshot=cast(SnapshotSupport, message.snapshot),
        snapshot_fanout=message.snapshot_fanout,
        pause_resume=message.pause_resume,
        gui=cast(GuiSupport, message.gui),
        gpu=message.gpu,
        port_expose=message.port_expose,
        host_mounts=message.host_mounts,
        enforces_limits=frozenset(cast(EnforcedLimit, item) for item in message.enforces_limits),
        max_concurrency=message.max_concurrency if message.HasField("max_concurrency") else None,
        max_lifetime_s=message.max_lifetime_s if message.HasField("max_lifetime_s") else None,
        max_resources=(
            _resources_from_pb(message.max_resources) if message.HasField("max_resources") else None
        ),
        typical_start_s=message.typical_start_s,
        reset_cost_s=message.reset_cost_s,
    )


def _optional(message: Any, name: str) -> str | None:
    return cast(str, getattr(message, name)) if message.HasField(name) else None


def profile_to_pb(profile: IsolationProfile) -> pb.IsolationProfile:
    message = pb.IsolationProfile(
        provider=profile.provider, declared_isolation=int(profile.declared_isolation)
    )
    if profile.verified_isolation is not None:
        message.verified_isolation = int(profile.verified_isolation)
    for key, entry in profile.invariants.items():
        item = message.invariants[key]
        item.how = entry.how
        item.waived = entry.waived
        for name in ("probe", "reason", "compensating", "approved_by", "expires"):
            value = getattr(entry, name)
            if value is not None:
                setattr(item, name, value)
    return message


def profile_from_pb(message: pb.IsolationProfile) -> IsolationProfile:
    return IsolationProfile(
        provider=message.provider,
        declared_isolation=Isolation(message.declared_isolation),
        verified_isolation=(
            Isolation(message.verified_isolation)
            if message.HasField("verified_isolation")
            else None
        ),
        invariants={
            key: InvariantEntry(
                how=item.how,
                probe=_optional(item, "probe"),
                waived=item.waived,
                reason=_optional(item, "reason"),
                compensating=_optional(item, "compensating"),
                approved_by=_optional(item, "approved_by"),
                expires=_optional(item, "expires"),
            )
            for key, item in message.invariants.items()
        },
    )
