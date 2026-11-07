"""gRPC channel and server options shared by both directions."""

from __future__ import annotations

MAX_MESSAGE_BYTES = 8 << 20
"""Above the 4 MiB transfer chunk plus framing; the gRPC default (4 MiB) would reject chunks."""

SERVER_OPTIONS: tuple[tuple[str, int], ...] = (
    ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
    ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
    # Clients ping every 30 s to notice a dead host mid-exec; allow it.
    ("grpc.http2.min_ping_interval_without_data_ms", 10_000),
)

CHANNEL_OPTIONS: tuple[tuple[str, int], ...] = (
    ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
    ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
    ("grpc.keepalive_time_ms", 30_000),
    ("grpc.keepalive_timeout_ms", 10_000),
)
