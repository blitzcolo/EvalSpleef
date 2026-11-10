"""Bounded backoff inside the judge client absorbs 429/5xx and timeouts per sample."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

import httpx
import pytest

from evalspleef.control.observe.judging import (
    JUDGE_RETRY_ATTEMPTS,
    GatewayJudgeClient,
)
from evalspleef.control.trial import classify_error
from evalspleef.spi.errors import EndpointUnavailable
from evalspleef.spi.gateway import COST_HEADER


class _Recorder:
    """Advances time only through the sleeps it records, so backoff is deterministic."""

    def __init__(self, random_values: Sequence[float] = (1.0,)) -> None:
        self.sleeps: list[float] = []
        self._now = 0.0
        self._random_values = iter(random_values)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds

    def monotonic(self) -> float:
        return self._now

    def random_source(self) -> float:
        return next(self._random_values, 1.0)


def _reply(
    status: int, *, retry_after: str | None = None, cost: str | None = None
) -> httpx.Response:
    headers: dict[str, str] = {}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    if cost is not None:
        headers[COST_HEADER] = cost
    if status == 200:
        body = {
            "choices": [{"message": {"content": json.dumps({"verdict": True})}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        return httpx.Response(200, json=body, headers=headers)
    return httpx.Response(status, json={"error": {"message": "boom"}}, headers=headers)


def _scripted(
    replies: list[httpx.Response | Exception],
) -> tuple[Callable[[httpx.Request], httpx.Response], Callable[[], int]]:
    """A transport handler that walks the script, repeating the last entry forever."""

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        item = replies[min(calls, len(replies) - 1)]
        calls += 1
        if isinstance(item, Exception):
            raise item
        return item

    return handler, lambda: calls


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    clock: _Recorder,
    *,
    retry_attempts: int = JUDGE_RETRY_ATTEMPTS,
    timeout_s: float = 60.0,
) -> GatewayJudgeClient:
    return GatewayJudgeClient(
        endpoint_id="ep_judge-1",
        gateway_url="http://gw.test/v1",
        provider="vendor-a",
        family="fam-a",
        transport=httpx.MockTransport(handler),
        timeout_s=timeout_s,
        retry_attempts=retry_attempts,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        random_source=clock.random_source,
    )


def test_429_then_success_succeeds_within_the_budget_and_charges_once() -> None:
    """Vertex express mode answered 429; the sample now absorbs it and books one charge."""
    handler, calls = _scripted([_reply(429), _reply(200, cost="0.25")])
    clock = _Recorder(random_values=(1.0,))
    client = _client(handler, clock)

    out = client.complete("x")

    assert json.loads(out)["verdict"] is True
    assert calls() == 2
    assert clock.sleeps == [1.0]  # full jitter: random(1.0) * base(1.0) * 2**0
    assert client.total_cost_usd == pytest.approx(0.25)  # only the successful attempt
    assert client.total_input_tokens == 10
    assert client.total_output_tokens == 5


def test_numeric_retry_after_overrides_the_backoff() -> None:
    handler, calls = _scripted([_reply(429, retry_after="7"), _reply(200)])
    clock = _Recorder()

    _client(handler, clock).complete("x")

    assert calls() == 2
    assert clock.sleeps == [7.0]


def test_503_twice_then_success_backs_off_exponentially() -> None:
    handler, calls = _scripted([_reply(503), _reply(503), _reply(200)])
    clock = _Recorder(random_values=(1.0, 1.0))

    _client(handler, clock).complete("x")

    assert calls() == 3
    assert clock.sleeps == [1.0, 2.0]  # 2**0, then 2**1


def test_connect_and_read_timeouts_are_retried_then_succeed() -> None:
    def timeout(exc: type[httpx.TimeoutException]) -> Exception:
        return exc(
            "upstream gone", request=httpx.Request("POST", "http://gw.test/v1/chat/completions")
        )

    handler, calls = _scripted(
        [timeout(httpx.ConnectTimeout), timeout(httpx.ReadTimeout), _reply(200)]
    )
    clock = _Recorder(random_values=(1.0, 1.0))

    _client(handler, clock).complete("x")

    assert calls() == 3
    assert clock.sleeps == [1.0, 2.0]


def test_after_the_retry_budget_it_raises_endpoint_unavailable() -> None:
    handler, calls = _scripted([_reply(429)])
    clock = _Recorder()

    with pytest.raises(EndpointUnavailable) as info:
        _client(handler, clock).complete("x")

    assert calls() == 1 + JUDGE_RETRY_ATTEMPTS
    assert len(clock.sleeps) == JUDGE_RETRY_ATTEMPTS
    assert classify_error(info.value).status == "failed_infra"


def test_a_400_is_not_retried() -> None:
    handler, calls = _scripted([_reply(400), _reply(200)])
    clock = _Recorder()

    with pytest.raises(httpx.HTTPStatusError):
        _client(handler, clock).complete("x")

    assert calls() == 1
    assert clock.sleeps == []


def test_a_retry_after_past_the_call_timeout_never_sleeps() -> None:
    """Even capped at 30s, the wait would blow this call's 25s budget: fail at once."""
    handler, calls = _scripted([_reply(429, retry_after="120"), _reply(200)])
    clock = _Recorder()

    with pytest.raises(EndpointUnavailable):
        _client(handler, clock, timeout_s=25.0).complete("x")

    assert calls() == 1
    assert clock.sleeps == []
