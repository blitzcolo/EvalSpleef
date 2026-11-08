"""Which isolation probes the command line waives before a batch preflights."""

from __future__ import annotations

from typing import Any

from evalspleef.control.cli.context import _waive_prose_probes
from evalspleef.spi.caps import Isolation
from evalspleef.spi.isolation import InvariantEntry, IsolationProfile


class _Instance:
    def __init__(self, invariants: dict[str, InvariantEntry]) -> None:
        self.instance, self.name, self.secrets = "p", "fake", None
        self.profile = IsolationProfile("p", Isolation.L1, invariants=invariants)

    def isolation_profile(self) -> IsolationProfile:
        return self.profile


class _Registry:
    def __init__(self, **instances: _Instance) -> None:
        self._sandbox_instances: dict[str, Any] = dict(instances)
        self._capabilities_cache: dict[str, Any] = {}
        self._isolation_cache: dict[str, Any] = {}

    def sandbox_instances(self) -> dict[str, Any]:
        return self._sandbox_instances


def test_only_described_preflight_probes_are_waived() -> None:
    commands = _Instance(
        {
            "I2": InvariantEntry(how="ro rootfs", probe="$ ! touch /.x"),
            "I10": InvariantEntry(how="roles", probe="verifier lease differs from the agent's"),
        }
    )
    described = _Instance({"I3": InvariantEntry(how="tmpfs", probe="/tmp is a tmpfs")})
    registry = _Registry(commands=commands, described=described)

    assert _waive_prose_probes(registry) == ["described"]  # type: ignore[arg-type]
    waived = registry.sandbox_instances()["described"].isolation_profile().invariants["I3"]
    assert waived.waived and waived.probe is None
