"""Execution helper using background read tasks and process group kill on timeout."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Mapping
from pathlib import Path

from evalspleef.spi.sandbox import TIMEOUT_EXIT_CODE, ExecResult, OutputCallback

_ACCUMULATION_LIMIT = 64 << 20


def _head_tail(data: bytes, budget: int) -> bytes:
    """Keep the head and tail of an over-long stream within budget (PRD convention)."""
    if len(data) <= budget:
        return data
    half = budget // 2
    return data[:half] + data[-half:]


async def run_process(
    cmd: str | list[str],
    cwd: Path,
    env: Mapping[str, str],
    stdin: bytes | None,
    timeout_s: float | None,
    on_output: OutputCallback | None,
    max_output_bytes: int,
) -> ExecResult:
    start = time.monotonic()
    is_shell = isinstance(cmd, str)

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    accumulated = 0
    truncated = False

    def _collect(chunks: list[bytes], data: bytes) -> None:
        nonlocal accumulated, truncated
        if accumulated < _ACCUMULATION_LIMIT:
            chunks.append(data)
            accumulated += len(data)
        else:
            truncated = True

    try:
        if is_shell:
            assert isinstance(cmd, str)
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=str(cwd),
                env=dict(env),
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        else:
            assert not isinstance(cmd, str)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                env=dict(env),
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
    except Exception as exc:
        return ExecResult(
            exit_code=1,
            stdout=b"",
            stderr=f"Failed to spawn process: {exc}\n".encode(),
            timed_out=False,
            duration_s=time.monotonic() - start,
        )

    async def _pump(name: str, chunks: list[bytes]) -> None:
        assert proc.stdout is not None
        stream = proc.stdout if name == "stdout" else proc.stderr
        assert stream is not None
        while True:
            data = await stream.read(65536)
            if not data:
                return
            _collect(chunks, data)
            if on_output is not None:
                on_output(name, data)  # type: ignore[arg-type]

    async def _feed_stdin() -> None:
        if stdin is None:
            return
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

    pumps = [
        asyncio.create_task(_pump("stdout", stdout_chunks)),
        asyncio.create_task(_pump("stderr", stderr_chunks)),
        asyncio.create_task(_feed_stdin()),
    ]

    timed_out = False
    try:
        if timeout_s is not None:
            await asyncio.wait_for(asyncio.gather(*pumps), timeout=timeout_s)
        else:
            await asyncio.gather(*pumps)
    except TimeoutError:
        timed_out = True
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        await asyncio.gather(*pumps, return_exceptions=True)
    finally:
        for t in pumps:
            t.cancel()

    exit_code = await proc.wait()
    if timed_out:
        exit_code = TIMEOUT_EXIT_CODE
    duration = time.monotonic() - start

    stdout = b"".join(stdout_chunks)
    stderr = b"".join(stderr_chunks)
    if len(stdout) + len(stderr) > max_output_bytes:
        truncated = True
        if stdout and stderr:
            per_stream = max_output_bytes // 2
            stdout = _head_tail(stdout, per_stream)
            stderr = _head_tail(stderr, per_stream)
        else:
            stdout = _head_tail(stdout, max_output_bytes)
            stderr = _head_tail(stderr, max_output_bytes)

    return ExecResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_s=duration,
        truncated=truncated,
    )
