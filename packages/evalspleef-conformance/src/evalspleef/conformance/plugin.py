"""pytest plugin for the conformance kit (TECH-DESIGN section 15.1).

Usage::

    pytest --spleef-provider=mock-sandbox --spleef-report=conf/report

``--spleef-provider NAME`` (repeatable) selects targets. Targets are discovered
from the ``evalspleef.conformance_targets`` entry-point group and from any
plugin module loaded with ``-p`` that implements ``pytest_spleef_targets``.
One test item is generated per (selected target, applicable case kind); the case
library is imported from ``CASE_MODULES`` at collection time.

A case that needs a capability the target does not declare is reported
not-applicable through its ``applies`` callable; a declared capability whose
case cannot run (for example a missing fault injector) fails with the
``AssertionError`` message the case raises. Nothing is silently passed.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable, Callable, Sequence
from importlib.metadata import entry_points
from typing import Any

import pytest

from evalspleef.conformance.case import Case
from evalspleef.conformance.report import CaseResult, ConformanceReport, TargetSummary
from evalspleef.conformance.target import ConformanceTarget

CASE_MODULES: tuple[str, ...] = (
    "evalspleef.conformance.cases_sandbox_basic",
    "evalspleef.conformance.cases_sandbox_env",
    "evalspleef.conformance.cases_model",
)
"""Case library modules, imported at collection time; a missing module is skipped."""


class SpleefHooks:
    """Extra hooks other plugin modules (for example ones loaded with ``-p``) may implement."""

    @pytest.hookspec
    def pytest_spleef_targets(self, config: pytest.Config) -> list[ConformanceTarget]:
        """Return additional conformance targets to run when ``--spleef-provider`` is used."""
        raise NotImplementedError


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    pluginmanager.add_hookspecs(SpleefHooks)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("spleef-conformance")
    group.addoption(
        "--spleef-provider",
        action="append",
        default=[],
        metavar="NAME",
        help="conformance target to run (repeatable); targets come from the "
        "evalspleef.conformance_targets entry points and from -p modules",
    )
    group.addoption(
        "--spleef-report",
        metavar="PATH",
        default=None,
        help="write the JSON and Markdown conformance report next to PATH",
    )


def load_cases() -> list[Case]:
    """Import ``CASES`` from every case module that ships with this installation."""
    cases: list[Case] = []
    for module_name in CASE_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # The case library is split across packages; a module that is not
            # shipped yet is skipped, an import error inside a shipped module is not.
            if exc.name is not None and (
                module_name == exc.name or module_name.startswith(f"{exc.name}.")
            ):
                continue
            raise
        cases.extend(module.CASES)
    return cases


def discover_targets(config: pytest.Config) -> list[ConformanceTarget]:
    """All targets from entry points plus ``pytest_spleef_targets`` hook implementations."""
    targets: list[ConformanceTarget] = []
    failures: dict[str, str] = {}
    for entry_point in entry_points(group="evalspleef.conformance_targets"):
        try:
            loaded = entry_point.load()
            produced = loaded()
        except ModuleNotFoundError as exc:
            # Adapters under development register their entry point before their
            # conformance module exists; warn and keep going instead of failing the run.
            config.issue_config_time_warning(
                pytest.PytestWarning(
                    f"conformance target entry point {entry_point.name!r} is not loadable "
                    f"({exc}); skipping it"
                ),
                stacklevel=2,
            )
            continue
        except Exception as exc:
            # Discovery only runs when targets are selected by name, so a failing adapter
            # must not take down a run that selects another one. The failure is reported as
            # a warning and, if the requested target is missing, in the usage error.
            failures[entry_point.name] = f"{type(exc).__name__}: {exc}"
            config.issue_config_time_warning(
                pytest.PytestWarning(
                    f"conformance target entry point {entry_point.name!r} failed "
                    f"({failures[entry_point.name]}); it is ignored"
                ),
                stacklevel=2,
            )
            continue
        targets.extend(produced)
    hook_results: list[list[ConformanceTarget]] = config.hook.pytest_spleef_targets(config=config)
    for produced in hook_results:
        targets.extend(produced)
    requested: Sequence[str] = config.getoption("--spleef-provider")
    if requested:
        by_name = {target.name: target for target in targets}
        unknown = [name for name in requested if name not in by_name]
        if unknown:
            known = ", ".join(sorted(by_name)) or "(none)"
            failed = "".join(
                f"; entry point {name!r} failed: {why}" for name, why in failures.items()
            )
            raise pytest.UsageError(
                f"unknown --spleef-provider {unknown!r}; known targets: {known}{failed}"
            )
        return [by_name[name] for name in requested]
    return targets


class ConformanceCollector(pytest.Collector):
    """Grouping node for generated conformance items."""

    def __init__(self, *, targets: list[ConformanceTarget], cases: list[Case], **kwargs: Any):
        super().__init__(**kwargs)
        self.targets = targets
        self.cases = cases

    def collect(self) -> list[pytest.Item]:
        items: list[pytest.Item] = []
        for target in self.targets:
            for case in self.cases:
                if case.kind != target.kind:
                    continue
                items.append(
                    ConformanceItem.from_parent(
                        self,
                        name=f"{target.name}::{case.id}",
                        target=target,
                        case=case,
                    )
                )
        return items


class ConformanceItem(pytest.Item):
    """One case against one target; the outcome is recorded for the report."""

    def __init__(self, *, target: ConformanceTarget, case: Case, **kwargs: Any):
        super().__init__(**kwargs)
        self.target = target
        self.case = case
        self.result: CaseResult | None = None

    def runtest(self) -> None:
        asyncio.run(self._run())
        if self.result is None:  # pragma: no cover - asyncio.run always records
            raise RuntimeError(f"conformance case {self.name} recorded no result")
        if self.result.status == "fail":
            raise AssertionError(self.result.detail)
        if self.result.status == "not-applicable":
            pytest.skip(self.result.detail)

    async def _run(self) -> None:
        case = self.case
        target = self.target
        reason = case.applies(target)
        if reason is not None:
            self.result = CaseResult(
                target=target.name,
                case_id=case.id,
                title=case.title,
                status="not-applicable",
                detail=reason,
                fake=target.fake,
            )
            return
        try:
            await case.run(target)
        except AssertionError as exc:
            self.result = CaseResult(
                target=target.name,
                case_id=case.id,
                title=case.title,
                status="fail",
                detail=str(exc),
                fake=target.fake,
            )
        except BaseException as exc:  # a case error is a failure, not a crash
            self.result = CaseResult(
                target=target.name,
                case_id=case.id,
                title=case.title,
                status="fail",
                detail=f"{type(exc).__name__}: {exc}",
                fake=target.fake,
            )
        else:
            self.result = CaseResult(
                target=target.name,
                case_id=case.id,
                title=case.title,
                status="pass",
                detail="",
                fake=target.fake,
            )

    def reportinfo(self) -> tuple[str, int, str]:
        return ("spleef-conformance", 0, f"conformance: {self.name}")


def _build_report(items: list[ConformanceItem]) -> ConformanceReport:
    """Project the recorded item results into a report."""
    results = tuple(item.result for item in items if item.result is not None)
    summaries: list[TargetSummary] = []
    seen: dict[str, TargetSummary] = {}
    for item in items:
        target = item.target
        if target.name in seen:
            continue
        target_results = [result for result in results if result.target == target.name]
        summary = TargetSummary(
            name=target.name,
            kind=target.kind,
            provider=type(target.provider).__name__,
            fake=target.fake,
            notes=target.notes,
            passed=sum(1 for result in target_results if result.status == "pass"),
            failed=sum(1 for result in target_results if result.status == "fail"),
            not_applicable=sum(1 for result in target_results if result.status == "not-applicable"),
        )
        seen[target.name] = summary
        summaries.append(summary)
    return ConformanceReport(targets=tuple(summaries), results=results)


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Add the generated conformance items before pytest counts what it collected."""
    requested: list[str] = config.getoption("--spleef-provider")
    if not requested:
        return
    targets = discover_targets(config)
    cases = load_cases()
    collector = ConformanceCollector.from_parent(
        session, name="spleef-conformance", targets=targets, cases=cases
    )
    items.extend(collector.collect())


def _run_cleanup(cleanup: Callable[[], Awaitable[None]]) -> None:
    """Drive one target cleanup coroutine to completion."""

    async def runner() -> None:
        await cleanup()

    asyncio.run(runner())


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Run target cleanup and write the report when requested."""
    items = [item for item in session.items if isinstance(item, ConformanceItem)]
    if not items:
        return
    cleaned: set[str] = set()
    for item in items:
        target = item.target
        if target.name in cleaned or target.cleanup is None:
            continue
        cleaned.add(target.name)
        _run_cleanup(target.cleanup)
    report_path = session.config.getoption("--spleef-report")
    if report_path is None:
        return
    report = _build_report(items)
    json_path, markdown_path = report.write(report_path)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        counts = report.counts()
        reporter.write_line(
            f"spleef-conformance: {counts['pass']} passed, {counts['fail']} failed, "
            f"{counts['not-applicable']} not-applicable; report: {json_path}, {markdown_path}"
        )
