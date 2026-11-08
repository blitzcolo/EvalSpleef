"""The probe suite P-01 .. P-15 (TECH-DESIGN section 6.4).

Each probe returns a :class:`ProbeCheck`; a ``passed`` flag means the capability was
observed, so optional capabilities (parallel tools, reasoning, ...) are simply False when
absent. Transport errors and malformed answers become failed checks instead of aborting the
run; once P-01 fails the remaining probes are recorded as skipped.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from evalspleef.spi.probe import ProbeCheck

from . import fixtures as fx
from .client import (
    CHAT_PATH,
    GatewayClient,
    ProbeResponseError,
    Reply,
    StreamReply,
    as_dict,
    as_list,
)
from .config import PROBE_IDS, ProbeConfig, ProbeTarget
from .protocols import probe_protocols

__all__ = ["ProbeSuite"]

_Probe = Callable[[], Awaitable[ProbeCheck]]
_REASONING_FIELDS = ("reasoning_content", "reasoning")
_TOO_LONG_STATUSES = (400, 413, 422)


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _percentile(values: list[float], quantile: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty sample."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(1, math.ceil(quantile * len(ordered))) - 1]


def _tool_calls(reply: Reply) -> list[dict[str, Any]]:
    return [as_dict(call) for call in as_list(reply.message.get("tool_calls"))]


def _call_name(call: dict[str, Any]) -> str:
    name = as_dict(call.get("function")).get("name")
    return name if isinstance(name, str) else ""


def _call_arguments(call: dict[str, Any]) -> object:
    return as_dict(call.get("function")).get("arguments")


def _reasoning_field(reply: Reply) -> str | None:
    for name in _REASONING_FIELDS:
        value = reply.message.get(name)
        if isinstance(value, str) and value.strip():
            return name
    return None


def _failed(probe_id: str, detail: str, **data: Any) -> ProbeCheck:
    return ProbeCheck(probe_id, False, detail, data)


class ProbeSuite:
    """Runs the probes of one endpoint through an authenticated gateway client."""

    def __init__(
        self,
        target: ProbeTarget,
        client: GatewayClient,
        config: ProbeConfig,
        *,
        now: Callable[[], float],
    ) -> None:
        self._t = target
        self._client = client
        self._cfg = config
        self._now = now

    # -- orchestration -----------------------------------------------------------

    def _selected(self, probe_id: str) -> bool:
        if probe_id == "P-15" and not self._cfg.enable_fingerprint:
            return False
        return self._cfg.include is None or probe_id in self._cfg.include

    def _probes(self) -> dict[str, _Probe]:
        return {
            "P-01": self._p01,
            "P-02": self._p02,
            "P-03": self._p03,
            "P-04": self._p04,
            "P-05": self._p05,
            "P-06": self._p06,
            "P-07": self._p07,
            "P-08": self._p08,
            "P-09": self._p09,
            "P-10": self._p10,
            "P-11": self._p11,
            "P-12": self._p12,
            "P-13": self._p13,
            "P-14": self._p14,
            "P-15": self._p15,
        }

    async def run(self) -> dict[str, ProbeCheck]:
        probes = self._probes()
        results: dict[str, ProbeCheck] = {}
        chat_failed = False
        for probe_id in PROBE_IDS:
            if not self._selected(probe_id):
                continue
            if chat_failed:
                results[probe_id] = _failed(probe_id, "skipped: P-01 failed", skipped=True)
                continue
            try:
                check = await probes[probe_id]()
            except (httpx.HTTPError, ProbeResponseError) as exc:
                check = _failed(probe_id, f"{type(exc).__name__}: {exc}", error=True)
            results[probe_id] = check
            if probe_id == "P-01" and not check.passed:
                chat_failed = True
        return results

    # -- request helpers -----------------------------------------------------------

    def _body(self, messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        return {"model": self._t.model_name, "messages": messages, **extra}

    async def _chat(
        self, messages: list[dict[str, Any]], *, timeout_s: float | None = None, **extra: Any
    ) -> Reply:
        return await self._client.post(
            CHAT_PATH, self._body(messages, **extra), timeout_s=timeout_s
        )

    # -- P-01 .. P-08 ---------------------------------------------------------------

    async def _p01(self) -> ProbeCheck:
        reply = await self._chat(
            [_user("Reply with the single word: ready.")], temperature=0, max_tokens=16
        )
        if not reply.ok:
            return _failed("P-01", reply.error, status=reply.status)
        usage = as_dict(reply.body.get("usage"))
        usage_returned = isinstance(usage.get("prompt_tokens"), int) and isinstance(
            usage.get("completion_tokens"), int
        )
        has_output = bool(reply.content.strip()) or _reasoning_field(reply) is not None
        return ProbeCheck(
            "P-01",
            has_output and usage_returned,
            "chat completion answered" if has_output else "empty assistant message",
            {
                "latency_s": reply.latency_s,
                "cold_start_ms": reply.cold_start_ms,
                "usage_returned": usage_returned,
            },
        )

    async def _p02(self) -> ProbeCheck:
        stream = await self._client.stream_chat(
            self._body(
                [_user("Count from one to five.")],
                temperature=0,
                max_tokens=64,
                stream=True,
                stream_options={"include_usage": True},
            )
        )
        if not stream.ok:
            return _failed("P-02", f"HTTP {stream.status} {stream.error}", status=stream.status)
        streaming = stream.done and stream.delta_chunks >= 1
        usage_in_stream = isinstance(stream.usage.get("completion_tokens"), int)
        return ProbeCheck(
            "P-02",
            streaming,
            "SSE stream completed" if streaming else "stream ended without content and [DONE]",
            {
                "streaming": streaming,
                "usage_in_stream": usage_in_stream,
                "ttft_s": stream.ttft_s,
                "delta_chunks": stream.delta_chunks,
            },
        )

    async def _p03(self) -> ProbeCheck:
        cities = fx.CITIES[: max(1, self._cfg.tool_samples)]
        valid = 0
        for city in cities:
            reply = await self._chat(
                [_user(fx.weather_prompt(city))],
                tools=[fx.WEATHER_TOOL],
                tool_choice="auto",
                temperature=0,
                max_tokens=256,
            )
            if not reply.ok:
                return _failed("P-03", reply.error, status=reply.status, valid_rate=0.0)
            calls = _tool_calls(reply)
            if calls and fx.valid_arguments(_call_arguments(calls[0]), "city"):
                valid += 1
        rate = valid / len(cities)
        return ProbeCheck(
            "P-03",
            rate >= self._cfg.tool_json_min_rate,
            f"{valid}/{len(cities)} samples produced a valid tool call",
            {"valid_rate": rate, "samples": len(cities)},
        )

    async def _p04(self) -> ProbeCheck:
        reply = await self._chat(
            [
                _user(
                    "I need two things at once: the weather in Paris and the local time in "
                    "Asia/Tokyo. Call both tools in the same turn."
                )
            ],
            tools=[fx.WEATHER_TOOL, fx.TIME_TOOL],
            tool_choice="auto",
            temperature=0,
            max_tokens=256,
        )
        if not reply.ok:
            return _failed("P-04", reply.error, status=reply.status)
        calls = _tool_calls(reply)
        names = {_call_name(call) for call in calls}
        ids = {call.get("id") for call in calls}
        parallel = len(calls) >= 2 and len(names) >= 2 and len(ids) == len(calls)
        return ProbeCheck(
            "P-04",
            parallel,
            f"{len(calls)} tool calls in one turn",
            {"tool_calls": len(calls)},
        )

    async def _p05(self) -> ProbeCheck:
        messages: list[dict[str, Any]] = [_user(fx.LOOKUP_START_PROMPT)]
        for index in range(fx.LOOKUP_ROUNDS):
            call_id = fx.lookup_call_id(index)
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": json.dumps(fx.lookup_arguments(index)),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": fx.lookup_result(index)}
            )
        messages.append(_user(fx.LOOKUP_SUMMARY_PROMPT))
        reply = await self._chat(
            messages, tools=[fx.LOOKUP_TOOL], tool_choice="none", temperature=0, max_tokens=256
        )
        if not reply.ok:
            return _failed("P-05", reply.error, status=reply.status)
        answered = bool(reply.content.strip()) and not _tool_calls(reply)
        return ProbeCheck(
            "P-05",
            answered,
            "text answer after three tool rounds"
            if answered
            else "no plain answer after tool history",
            {"rounds": fx.LOOKUP_ROUNDS},
        )

    async def _p06(self) -> ProbeCheck:
        modes: list[str] = []
        required = await self._chat(
            [_user("Say hello.")],
            tools=[fx.WEATHER_TOOL],
            tool_choice="required",
            temperature=0,
            max_tokens=128,
        )
        if required.ok and _tool_calls(required):
            modes.append("required")
        forbidden = await self._chat(
            [_user("What is the weather in Paris? Do not use tools, just answer.")],
            tools=[fx.WEATHER_TOOL],
            tool_choice="none",
            temperature=0,
            max_tokens=64,
        )
        if forbidden.ok and not _tool_calls(forbidden) and forbidden.content.strip():
            modes.append("none")
        return ProbeCheck(
            "P-06",
            len(modes) == 2,
            f"tool_choice modes honoured: {modes}",
            {"modes": modes, "statuses": [required.status, forbidden.status]},
        )

    async def _p07(self) -> ProbeCheck:
        reply = await self._chat(
            [
                _user(
                    "Answer in JSON: what is the capital of France? Give a confidence from 0 to 10."
                )
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "answer", "strict": True, "schema": fx.ANSWER_SCHEMA},
            },
            temperature=0,
            max_tokens=128,
        )
        if not reply.ok:
            return _failed("P-07", reply.error, status=reply.status)
        try:
            parsed = as_dict(json.loads(reply.content))
        except ValueError:
            parsed = {}
        valid = isinstance(parsed.get("answer"), str) and isinstance(parsed.get("confidence"), int)
        return ProbeCheck(
            "P-07",
            valid,
            "strict json_schema honoured" if valid else "reply is not schema-valid JSON",
        )

    async def _p08(self) -> ProbeCheck:
        prompt = [_user("What is 17 * 23? Think step by step.")]
        reply = await self._chat(prompt, temperature=0, max_tokens=512)
        if not reply.ok:
            return _failed("P-08", reply.error, status=reply.status)
        field = _reasoning_field(reply)
        toggle = False
        if field is not None:
            off = await self._chat(
                prompt, temperature=0, max_tokens=512, reasoning={"enabled": False}
            )
            toggle = off.ok and _reasoning_field(off) is None
        return ProbeCheck(
            "P-08",
            True,
            f"reasoning field {field!r}, toggle {toggle}" if field else "no reasoning field",
            {"reasoning_field": field, "reasoning_toggle": toggle},
        )

    # -- P-09 .. P-12 -----------------------------------------------------------------

    async def _recalls(self, tokens: int) -> bool:
        """Place a needle at the start of a ~``tokens``-token prompt and ask for it back."""
        code = (
            "NDL"
            + hashlib.sha256(f"{self._t.endpoint_id}:{tokens}".encode()).hexdigest()[:8].upper()
        )
        filler = " ".join(["the"] * tokens)
        prompt = (
            f"Remember this secret code. needle: {code}\n\n{filler}\n\n"
            "What was the secret code? Answer with the code only."
        )
        reply = await self._chat(
            [_user(prompt)],
            temperature=0,
            max_tokens=self._cfg.needle_max_tokens,
            timeout_s=self._cfg.long_request_timeout_s,
        )
        if reply.status in _TOO_LONG_STATUSES:
            return False
        if not reply.ok:
            raise ProbeResponseError(f"context probe at {tokens} tokens failed: {reply.error}")
        reasoning = _reasoning_field(reply)
        recalled_in = [reply.content, str(reply.message.get(reasoning, "")) if reasoning else ""]
        return any(code in text for text in recalled_in)

    async def _p09(self) -> ProbeCheck:
        declared = self._t.declared_context_tokens
        requests = 0

        async def recalls(tokens: int) -> bool:
            nonlocal requests
            requests += 1
            return await self._recalls(tokens)

        if await recalls(declared):
            effective = declared
        else:
            low = min(declared, self._cfg.context_floor_tokens)
            high = declared
            if not await recalls(low):
                effective = 0
            else:
                while high - low > self._cfg.context_resolution_tokens:
                    middle = (low + high) // 2
                    if await recalls(middle):
                        low = middle
                    else:
                        high = middle
                effective = low
        return ProbeCheck(
            "P-09",
            effective > 0,
            f"needle recalled up to {effective} of {declared} declared tokens",
            {"effective": effective, "declared": declared, "requests": requests},
        )

    async def _p10(self) -> ProbeCheck:
        declared = self._t.declared_max_output_tokens
        cap = self._cfg.max_output_probe_tokens
        requested = min(declared, cap) if cap else declared
        reply: Reply | None = None
        for attempt in range(self._cfg.max_output_halvings + 1):
            reply = await self._chat(
                [_user(fx.LONG_OUTPUT_PROMPT)],
                temperature=0,
                max_tokens=requested,
                timeout_s=self._cfg.long_request_timeout_s,
            )
            if reply.status != 400 or attempt == self._cfg.max_output_halvings or requested <= 1:
                break
            requested //= 2
        assert reply is not None
        if not reply.ok:
            return _failed("P-10", reply.error, status=reply.status, requested=requested)
        produced = reply.completion_tokens or len(reply.content.split())
        limited_by = "max_tokens" if reply.finish_reason == "length" else "model"
        return ProbeCheck(
            "P-10",
            produced > 0,
            f"{produced} tokens produced (finish_reason={reply.finish_reason})",
            {
                "effective": produced,
                "requested": requested,
                "finish_reason": reply.finish_reason,
                "limited_by": limited_by,
            },
        )

    async def _p11(self) -> ProbeCheck:
        sentence = [_user("Write one short sentence about the sea.")]
        first = await self._chat(sentence, temperature=0.8, seed=1234, max_tokens=48)
        second = await self._chat(sentence, temperature=0.8, seed=1234, max_tokens=48)
        seed_ok = first.ok and second.ok and bool(first.content) and first.content == second.content
        control = await self._chat([_user(fx.STOP_PROMPT)], temperature=0, max_tokens=32)
        stopped = await self._chat(
            [_user(fx.STOP_PROMPT)], temperature=0, max_tokens=32, stop=[fx.STOP_SEQUENCE]
        )
        stop_ok = (
            control.ok
            and fx.STOP_SEQUENCE in control.content
            and stopped.ok
            and fx.STOP_SEQUENCE not in stopped.content
            and bool(stopped.content.strip())
        )
        return ProbeCheck(
            "P-11",
            seed_ok and stop_ok,
            f"seed deterministic={seed_ok}, stop honoured={stop_ok}",
            {"seed_deterministic": seed_ok, "stop_ok": stop_ok},
        )

    async def _timed_stream(self, index: int) -> StreamReply | None:
        try:
            return await self._client.stream_chat(
                self._body(
                    [_user(f"Write a short sentence about the number {index}.")],
                    temperature=0,
                    max_tokens=self._cfg.throughput_max_tokens,
                    stream=True,
                    stream_options={"include_usage": True},
                )
            )
        except (httpx.HTTPError, ProbeResponseError):
            return None

    async def _p12(self) -> ProbeCheck:
        throughput: dict[str, dict[str, float]] = {}
        errors = 0
        for concurrency in self._cfg.throughput_concurrency:
            gate = asyncio.Semaphore(concurrency)

            async def one(index: int, gate: asyncio.Semaphore = gate) -> StreamReply | None:
                async with gate:
                    return await self._timed_stream(index)

            replies = await asyncio.gather(*(one(i) for i in range(self._cfg.throughput_requests)))
            good = [r for r in replies if r is not None and r.ok and r.done]
            errors += len(replies) - len(good)
            ttfts = [r.ttft_s if r.ttft_s is not None else r.latency_s for r in good]
            rates: list[float] = []
            for reply in good:
                tokens = reply.usage.get("completion_tokens")
                count = tokens if isinstance(tokens, int) else reply.delta_chunks
                generation_s = reply.latency_s - (reply.ttft_s or 0.0)
                span = generation_s if generation_s > 0 else reply.latency_s
                rates.append(count / span if span > 0 else 0.0)
            throughput[str(concurrency)] = {
                "ttft_p50": _percentile(ttfts, 0.50),
                "ttft_p95": _percentile(ttfts, 0.95),
                "tps_p50": _percentile(rates, 0.50),
                "tps_p95": _percentile(rates, 0.95),
            }
        return ProbeCheck(
            "P-12",
            errors == 0,
            f"{errors} failed requests" if errors else "all throughput requests succeeded",
            {"throughput": throughput, "errors": errors},
        )

    # -- P-13 .. P-15 -------------------------------------------------------------------

    async def _p13(self) -> ProbeCheck:
        return await probe_protocols(
            self._client, self._t.model_name, timeout_s=self._cfg.request_timeout_s
        )

    async def _p14(self) -> ProbeCheck:
        reply = await self._chat([_user("Reply with: ok")], temperature=0, max_tokens=4)
        if not reply.ok:
            return _failed("P-14", reply.error, status=reply.status)
        model = reply.body.get("model")
        fingerprint = reply.body.get("system_fingerprint")
        model_ok = isinstance(model, str) and model in self._t.expected_models
        expected = self._t.expected_system_fingerprint
        fingerprint_ok = (
            expected is None or not isinstance(fingerprint, str) or fingerprint == expected
        )
        return ProbeCheck(
            "P-14",
            model_ok and fingerprint_ok,
            "response identity matches registration"
            if model_ok and fingerprint_ok
            else f"response model {model!r} / fingerprint {fingerprint!r} differ from registration",
            {
                "response_model": model if isinstance(model, str) else None,
                "system_fingerprint": fingerprint if isinstance(fingerprint, str) else None,
                "expected_models": sorted(self._t.expected_models),
            },
        )

    async def _p15(self) -> ProbeCheck:
        digest = hashlib.sha256()
        for prompt in fx.FINGERPRINT_PROMPTS:
            reply = await self._chat([_user(prompt)], temperature=0, seed=0, max_tokens=64)
            if not reply.ok:
                return _failed("P-15", reply.error, status=reply.status)
            digest.update(reply.content.encode("utf-8"))
            digest.update(b"\x00")
        fingerprint = digest.hexdigest()
        reference = self._cfg.fingerprint_reference
        matches = None if reference is None else fingerprint == reference
        return ProbeCheck(
            "P-15",
            matches is not False,
            "fingerprint collected" if matches is None else f"matches reference: {matches}",
            {"fingerprint": fingerprint, "matches_reference": matches},
        )
