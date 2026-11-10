"""The thread bridge between the synchronous judge panel and the async sandbox API.

``JudgePanel`` calls ``complete()`` from a worker thread (the evaluator runs it with
``asyncio.to_thread``). Each call runs one whole sample as a single coroutine on the event
loop, submitted with :func:`asyncio.run_coroutine_threadsafe` and awaited with a bounded
``result(timeout)``. Keeping the sandbox lifecycle inside that one coroutine means the
``async with`` that destroys the sandbox runs on the loop whatever happens: a normal end, a
failure, the bridge timeout, or the evaluator being cancelled (:meth:`SampleRunner.cancel`
cancels and awaits every sample still on the loop).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from evalspleef.spi.errors import (
    CapabilityNotSupported,
    EndpointUnavailable,
    PolicyNotEnforceable,
    SpleefError,
)
from evalspleef.spi.sandbox import Sandbox, WritablePath

from ..batch.executor import PlannedTrial
from .agent_records import SampleRecord
from .agent_session import ARTIFACT_DIR, AgentJudgeLimits, AgentSession, SampleSpec

__all__ = [
    "AgentJudgeCancelled",
    "JudgeSandboxOpener",
    "JudgeSandboxRequest",
    "SampleRunner",
]

#: Head room over the sample's wall clock for boot, upload, integrity check and teardown.
BRIDGE_GRACE_S = 180.0


class AgentJudgeCancelled(Exception):
    """The evaluation was cancelled; the panel thread stops at its next sample."""


@dataclass(frozen=True)
class JudgeSandboxRequest:
    """One judge sandbox: role ``judge``, the trial's own template, network off."""

    trial: PlannedTrial
    item_id: str
    sample: str
    """``<vendor>#<n>``, unique per item within one evaluation."""
    writable_paths: tuple[WritablePath, ...]


class JudgeSandboxOpener(Protocol):
    """Creates a fresh judge sandbox and destroys it when the context exits."""

    def open(self, request: JudgeSandboxRequest) -> AbstractAsyncContextManager[Sandbox]: ...


@dataclass(frozen=True)
class RunSetup:
    """Everything a sample of one evaluation shares."""

    opener: JudgeSandboxOpener
    trial: PlannedTrial
    tree: Path
    """The sanitised export uploaded read-only to ``/artifact``."""
    expected: Mapping[str, str]
    """Host-side manifest of ``tree`` the in-sandbox hash must equal."""
    zones: tuple[WritablePath, ...]
    limits: AgentJudgeLimits


def failed_record(spec: SampleSpec, reason: str) -> SampleRecord:
    return SampleRecord(spec.vendor, spec.index, spec.session_id, "inconclusive", reason)


class SampleRunner:
    """Runs samples on ``loop`` for panel threads and keeps their records.

    Build it on the loop's own thread: that thread is the one ``run_sample`` refuses.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, setup: RunSetup) -> None:
        self._loop = loop
        self._loop_thread = threading.get_ident()
        self._setup = setup
        self._cancelled = threading.Event()
        self._tasks: set[asyncio.Task[SampleRecord]] = set()
        self._lock = threading.Lock()
        self.records: list[SampleRecord] = []

    def run_sample(self, session: AgentSession, spec: SampleSpec) -> SampleRecord:
        """Blocking; call from a worker thread, never from the loop's own thread."""
        if threading.get_ident() == self._loop_thread:
            raise RuntimeError("run_sample would deadlock on the event loop thread")
        if self._cancelled.is_set():
            raise AgentJudgeCancelled("agent judge cancelled")
        future = asyncio.run_coroutine_threadsafe(self._sample(session, spec), self._loop)
        try:
            record = future.result(timeout=self._setup.limits.wall_clock_s + BRIDGE_GRACE_S)
        except concurrent.futures.TimeoutError:
            # Cancelling the task runs its teardown on the loop; drain() waits for it.
            future.cancel()
            record = failed_record(spec, "sample did not finish within the bridge timeout")
        with self._lock:
            self.records.append(record)
        return record

    async def _sample(self, session: AgentSession, spec: SampleSpec) -> SampleRecord:
        task = asyncio.current_task()
        assert task is not None  # a coroutine scheduled by run_coroutine_threadsafe
        self._tasks.add(task)
        try:
            if self._cancelled.is_set():
                return failed_record(spec, "agent judge cancelled")
            request = JudgeSandboxRequest(
                self._setup.trial, spec.item.id, f"{spec.vendor}#{spec.index}", self._setup.zones
            )
            started = time.monotonic()
            try:
                async with self._setup.opener.open(request) as sandbox:
                    await sandbox.upload(self._setup.tree, ARTIFACT_DIR, read_only=True)
                    boot_s = round(time.monotonic() - started, 3)
                    record = await session.run(sandbox, spec, self._setup.expected)
                    return replace(record, boot_s=boot_s)
            except (CapabilityNotSupported, PolicyNotEnforceable, EndpointUnavailable):
                raise
            except SpleefError as exc:
                return failed_record(spec, f"judge sandbox: {type(exc).__name__}: {exc}")
        finally:
            self._tasks.discard(task)

    async def cancel(self) -> None:
        """Stop new samples, cancel the running ones and wait for their teardown."""
        self._cancelled.set()
        for task in tuple(self._tasks):
            task.cancel()
        await self.drain()

    async def drain(self) -> None:
        """Wait until no sample is left on the loop (every sandbox destroyed)."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
