"""``spleef-egress`` command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from collections.abc import Sequence

from .policy import Policy
from .proxy import EgressProxy, split_authority


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spleef-egress",
        description="Allowlist forward proxy for one trial, with URL and DNS audit logs.",
    )
    parser.add_argument("--policy", required=True, metavar="FILE", help="policy YAML file")
    parser.add_argument(
        "--listen",
        default="127.0.0.1:3128",
        metavar="HOST:PORT",
        help="listen address; port 0 picks a free port (default: 127.0.0.1:3128)",
    )
    parser.add_argument(
        "--log-dir",
        required=True,
        metavar="DIR",
        help="directory for url.jsonl and dns.jsonl (outside the sandbox)",
    )
    parser.add_argument(
        "--trial-id",
        required=True,
        metavar="ID",
        help="trial id stamped on every log line",
    )
    return parser


async def _serve(policy: Policy, host: str, port: int, log_dir: str, trial_id: str) -> None:
    proxy = EgressProxy(policy, host=host, port=port, log_dir=log_dir, trial_id=trial_id)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    await proxy.start()
    bind_host, bind_port = proxy.address
    print(f"spleef-egress listening on {bind_host}:{bind_port}", flush=True)
    try:
        await stop.wait()
    finally:
        await proxy.stop()


def main(argv: Sequence[str] | None = None) -> None:
    """Parse arguments, load the policy and serve until SIGINT/SIGTERM."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        policy = Policy.from_yaml(args.policy)
        host, port = split_authority(args.listen, 3128)
    except ValueError as exc:
        parser.error(str(exc))
    asyncio.run(_serve(policy, host, port, args.log_dir, args.trial_id))
