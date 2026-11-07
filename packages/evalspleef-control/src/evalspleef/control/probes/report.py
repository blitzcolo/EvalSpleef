"""Probe checks to ``ModelCapabilities`` and the immutable, content-addressed ``ProbeReport``."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from evalspleef.control.store.rows import ProbeReportRow
from evalspleef.spi.probe import ModelCapabilities, ProbeCheck, ProbeReport, ToolCallingLevel

from .client import as_dict, as_list
from .config import PROBE_IDS

__all__ = [
    "build_capabilities",
    "build_report",
    "capabilities_from_dict",
    "capabilities_to_dict",
    "compute_report_id",
    "report_from_row",
    "report_to_row",
]


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _str_set(value: object) -> frozenset[str]:
    return frozenset(item for item in as_list(value) if isinstance(item, str))


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def build_capabilities(checks: Mapping[str, ProbeCheck]) -> ModelCapabilities:
    """Fold probe outputs into capabilities; probes that did not run keep their defaults."""

    def data(probe_id: str) -> Mapping[str, Any]:
        check = checks.get(probe_id)
        return check.data if check is not None else {}

    def passed(probe_id: str) -> bool:
        check = checks.get(probe_id)
        return check is not None and check.passed

    tool_calling: ToolCallingLevel = (
        "parallel" if passed("P-04") else "single" if passed("P-03") else "none"
    )
    protocols = set(_str_set(data("P-13").get("protocols")))
    if passed("P-01"):
        protocols.add("chat_completions")
    throughput = {
        str(concurrency): {str(metric): _float(value) for metric, value in as_dict(stats).items()}
        for concurrency, stats in as_dict(data("P-12").get("throughput")).items()
    }
    return ModelCapabilities(
        chat=passed("P-01"),
        streaming=bool(data("P-02").get("streaming")),
        usage_in_stream=bool(data("P-02").get("usage_in_stream")),
        tool_calling=tool_calling,
        tool_call_json_valid_rate=_float(data("P-03").get("valid_rate")),
        multi_turn_tools=passed("P-05"),
        tool_choice_modes=_str_set(data("P-06").get("modes")),
        json_schema=passed("P-07"),
        reasoning_field=_optional_str(data("P-08").get("reasoning_field")),
        reasoning_toggle=bool(data("P-08").get("reasoning_toggle")),
        context_tokens_effective=_int(data("P-09").get("effective")),
        max_output_tokens_effective=_int(data("P-10").get("effective")),
        seed_deterministic=bool(data("P-11").get("seed_deterministic")),
        stop_ok=bool(data("P-11").get("stop_ok")),
        protocols_ok=frozenset(protocols),
        identity_ok=passed("P-14"),
        throughput=throughput,
        fingerprint=_optional_str(data("P-15").get("fingerprint")),
    )


def _check_to_dict(check: ProbeCheck) -> dict[str, Any]:
    return {
        "probe_id": check.probe_id,
        "passed": check.passed,
        "detail": check.detail,
        "data": dict(check.data),
    }


def _check_from_dict(raw: Mapping[str, Any]) -> ProbeCheck:
    return ProbeCheck(
        probe_id=str(raw.get("probe_id", "")),
        passed=bool(raw.get("passed")),
        detail=str(raw.get("detail", "")),
        data=as_dict(raw.get("data")),
    )


def compute_report_id(
    endpoint_id: str, gateway_version: str, created_at: str, checks: tuple[ProbeCheck, ...]
) -> str:
    """Stable id: identical inputs always hash to the same ``pr_<16 hex>``."""
    canonical = json.dumps(
        {
            "endpoint_id": endpoint_id,
            "gateway_version": gateway_version,
            "created_at": created_at,
            "checks": [_check_to_dict(check) for check in checks],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "pr_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_report(
    endpoint_id: str,
    gateway_version: str,
    created_at: str,
    checks: Mapping[str, ProbeCheck],
) -> ProbeReport:
    ordered = tuple(checks[probe_id] for probe_id in PROBE_IDS if probe_id in checks)
    return ProbeReport(
        report_id=compute_report_id(endpoint_id, gateway_version, created_at, ordered),
        endpoint_id=endpoint_id,
        gateway_version=gateway_version,
        created_at=created_at,
        checks=ordered,
        capabilities=build_capabilities(checks),
    )


def capabilities_to_dict(caps: ModelCapabilities) -> dict[str, Any]:
    raw = dataclasses.asdict(caps)
    raw["tool_choice_modes"] = sorted(caps.tool_choice_modes)
    raw["protocols_ok"] = sorted(caps.protocols_ok)
    raw["throughput"] = {key: dict(stats) for key, stats in caps.throughput.items()}
    return raw


def capabilities_from_dict(raw: Mapping[str, Any]) -> ModelCapabilities:
    level = raw.get("tool_calling")
    tool_calling: ToolCallingLevel = (
        "parallel" if level == "parallel" else "single" if level == "single" else "none"
    )
    return ModelCapabilities(
        chat=bool(raw.get("chat")),
        streaming=bool(raw.get("streaming")),
        usage_in_stream=bool(raw.get("usage_in_stream")),
        tool_calling=tool_calling,
        tool_call_json_valid_rate=_float(raw.get("tool_call_json_valid_rate")),
        multi_turn_tools=bool(raw.get("multi_turn_tools")),
        tool_choice_modes=_str_set(raw.get("tool_choice_modes")),
        json_schema=bool(raw.get("json_schema")),
        reasoning_field=_optional_str(raw.get("reasoning_field")),
        reasoning_toggle=bool(raw.get("reasoning_toggle")),
        context_tokens_effective=_int(raw.get("context_tokens_effective")),
        max_output_tokens_effective=_int(raw.get("max_output_tokens_effective")),
        seed_deterministic=bool(raw.get("seed_deterministic")),
        stop_ok=bool(raw.get("stop_ok")),
        protocols_ok=_str_set(raw.get("protocols_ok")),
        identity_ok=bool(raw.get("identity_ok")),
        throughput={
            str(key): {str(metric): _float(value) for metric, value in as_dict(stats).items()}
            for key, stats in as_dict(raw.get("throughput")).items()
        },
        fingerprint=_optional_str(raw.get("fingerprint")),
    )


def report_to_row(report: ProbeReport) -> ProbeReportRow:
    return ProbeReportRow(
        report_id=report.report_id,
        endpoint_id=report.endpoint_id,
        gateway_version=report.gateway_version,
        results={"checks": [_check_to_dict(check) for check in report.checks]},
        capabilities=capabilities_to_dict(report.capabilities),
        created_at=report.created_at,
    )


def report_from_row(row: ProbeReportRow) -> ProbeReport:
    return ProbeReport(
        report_id=row.report_id,
        endpoint_id=row.endpoint_id,
        gateway_version=row.gateway_version,
        created_at=row.created_at,
        checks=tuple(_check_from_dict(as_dict(raw)) for raw in as_list(row.results.get("checks"))),
        capabilities=capabilities_from_dict(row.capabilities),
    )
