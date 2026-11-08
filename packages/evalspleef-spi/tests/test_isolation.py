"""Probe conventions of the isolation profile."""

from __future__ import annotations

from evalspleef.spi.isolation import InvariantEntry


def test_a_dollar_prefixed_probe_is_a_command() -> None:
    assert InvariantEntry(how="h", probe="$ test -d /tmp").command == "test -d /tmp"


def test_a_described_probe_has_no_command() -> None:
    assert InvariantEntry(how="h", probe="checked by conformance C-LIFE-01").command is None
    assert InvariantEntry(how="h").command is None
