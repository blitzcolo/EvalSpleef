"""Fixed prompts, tool schemas and scripts used by the probes (deterministic by design)."""

from __future__ import annotations

import json
from typing import Any

from .client import as_dict

WEATHER_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {"city": {"type": "string", "description": "City name"}},
    "required": ["city"],
}
TIME_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {"timezone": {"type": "string", "description": "IANA timezone"}},
    "required": ["timezone"],
}
LOOKUP_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {"key": {"type": "string"}},
    "required": ["key"],
}
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "confidence": {"type": "integer"}},
    "required": ["answer", "confidence"],
    "additionalProperties": False,
}

WEATHER_DESCRIPTION = "Get the current weather for a city."
TIME_DESCRIPTION = "Get the current local time in a timezone."
LOOKUP_DESCRIPTION = "Look up the value stored under a key."


def chat_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


WEATHER_TOOL = chat_tool("get_weather", WEATHER_DESCRIPTION, WEATHER_PARAMS)
TIME_TOOL = chat_tool("get_time", TIME_DESCRIPTION, TIME_PARAMS)
LOOKUP_TOOL = chat_tool("lookup", LOOKUP_DESCRIPTION, LOOKUP_PARAMS)

CITIES = ("Paris", "Tokyo", "Cairo", "Lima", "Oslo")
LOOKUP_ROUNDS = 3
LOOKUP_START_PROMPT = "Look up three stored items one after another, then report them."
LOOKUP_SUMMARY_PROMPT = (
    "Now summarise the three looked-up values in one sentence. Do not call any tool."
)
LONG_OUTPUT_PROMPT = (
    "Write a very long essay about the history of computing. Do not stop early: keep writing "
    "until you run out of space."
)
STOP_PROMPT = "Repeat the following line exactly and nothing else: needle: alphaENDomega"
STOP_SEQUENCE = "END"
FINGERPRINT_PROMPTS = tuple(
    f"Fingerprint prompt {number}: name one prime number." for number in range(20)
)


def weather_prompt(city: str) -> str:
    return f"What is the weather in {city} right now? Use the get_weather tool."


def lookup_call_id(round_index: int) -> str:
    return f"call_lookup_{round_index}"


def lookup_arguments(round_index: int) -> dict[str, str]:
    return {"key": f"item{round_index}"}


def lookup_result(round_index: int) -> str:
    return json.dumps({"value": round_index * 7})


def valid_arguments(arguments: object, required: str) -> bool:
    """True when ``arguments`` (JSON text or object) is an object with a non-empty string field."""
    parsed: object = arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return False
    value = as_dict(parsed).get(required)
    return isinstance(value, str) and value != ""
