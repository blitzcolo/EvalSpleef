"""Pure protocol translators for the model gateway.

The gateway exposes OpenAI Chat Completions, OpenAI Responses (P1) and Anthropic Messages
while every upstream endpoint speaks Chat Completions (TECH-DESIGN section 6.3,
FR-MOD-07). These modules hold the translation layer only: no I/O, no FastAPI or other
web-framework imports, so every converter can be unit-tested and reused by the HTTP
application.

Public function table
---------------------

``anthropic.py``
    ``anthropic_request_to_chat(body)``
        Anthropic Messages request -> OpenAI Chat Completions request.
    ``chat_response_to_anthropic(resp, *, model)``
        Non-streaming Chat Completions response -> Anthropic message.
    ``ChatToAnthropicStream(model=...)``
        Streaming converter; ``feed(chunk)`` returns Anthropic SSE event strings and
        ``finish()`` closes the message.

``responses.py``
    ``responses_request_to_chat(body)``
        OpenAI Responses request -> OpenAI Chat Completions request.
    ``chat_response_to_responses(resp, *, model)``
        Non-streaming Chat Completions response -> Responses object.
    ``ChatToResponsesStream(model=...)``
        Streaming converter; ``feed(chunk)`` returns Responses SSE event strings and
        ``finish()`` emits ``response.completed``.

``errors.py``
    ``anthropic_error_envelope(status, code, message)``
        Upstream error -> Anthropic error envelope.
    ``openai_error_envelope(status, code, message)``
        Upstream error -> OpenAI error envelope, preserving ``cost_cap`` / ``token_cap``.
    ``anthropic_error_type(status, code)`` / ``openai_error_type(status, code)``
        The error type selected for a status and code.
"""

from __future__ import annotations

from .anthropic import ChatToAnthropicStream, anthropic_request_to_chat, chat_response_to_anthropic
from .anthropic_upstream import (
    AnthropicToChatStream,
    anthropic_response_to_chat,
    chat_request_to_anthropic,
)
from .errors import (
    BUDGET_CODES,
    anthropic_error_envelope,
    anthropic_error_type,
    openai_error_envelope,
    openai_error_type,
)
from .responses import ChatToResponsesStream, chat_response_to_responses, responses_request_to_chat

PUBLIC_FUNCTION_TABLE: dict[str, str] = {
    "anthropic_request_to_chat": "Anthropic Messages request -> Chat Completions request",
    "chat_response_to_anthropic": "Chat Completions response -> Anthropic message",
    "ChatToAnthropicStream": "Chat Completions chunks -> Anthropic SSE events",
    "responses_request_to_chat": "Responses request -> Chat Completions request",
    "chat_response_to_responses": "Chat Completions response -> Responses object",
    "ChatToResponsesStream": "Chat Completions chunks -> Responses SSE events",
    "anthropic_error_envelope": "Upstream error -> Anthropic error envelope",
    "openai_error_envelope": "Upstream error -> OpenAI error envelope",
    "anthropic_error_type": "Upstream status/code -> Anthropic error type",
    "openai_error_type": "Upstream status/code -> OpenAI error type",
}

__all__ = [
    "BUDGET_CODES",
    "PUBLIC_FUNCTION_TABLE",
    "AnthropicToChatStream",
    "ChatToAnthropicStream",
    "ChatToResponsesStream",
    "anthropic_error_envelope",
    "anthropic_error_type",
    "anthropic_request_to_chat",
    "anthropic_response_to_chat",
    "chat_request_to_anthropic",
    "chat_response_to_anthropic",
    "chat_response_to_responses",
    "openai_error_envelope",
    "openai_error_type",
    "responses_request_to_chat",
]
