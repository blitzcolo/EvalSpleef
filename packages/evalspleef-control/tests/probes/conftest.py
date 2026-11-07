"""Fixtures: a FakeOpenAIServer behind the in-process gateway stand-in."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from evalspleef.spi.model import UpstreamRoute
from evalspleef.testing.fake_gateway import InMemoryGatewayAdmin
from evalspleef.testing.fake_openai import FakeOpenAIServer

_MODULE = "evalspleef_control_tests_gateway_standin"


def load_standin() -> Any:
    """Import the stand-in module by path (tests directories are not importable packages)."""
    if _MODULE not in sys.modules:
        path = Path(__file__).parent / "gateway_standin.py"
        spec = importlib.util.spec_from_file_location(_MODULE, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE] = module
        spec.loader.exec_module(module)
    return sys.modules[_MODULE]


@dataclass
class Stack:
    admin: InMemoryGatewayAdmin
    standin: Any
    server: FakeOpenAIServer
    route_name: str


@pytest.fixture
def make_stack() -> Iterator[Callable[..., Stack]]:
    """Factory: start a fake upstream and a gateway stand-in with one registered route."""
    with ExitStack() as exit_stack:

        def make(
            *, route_name: str = "ep_test", cold_start_window_s: int = 0, **server_kwargs: Any
        ) -> Stack:
            server = exit_stack.enter_context(FakeOpenAIServer(**server_kwargs))
            admin = InMemoryGatewayAdmin()
            admin.routes[route_name] = UpstreamRoute(
                api_base=server.base_url,
                model=server.model,
                protocol="chat_completions",
                api_key_ref="vault://models/test",
                cold_start_window_s=cold_start_window_s,
            )
            standin = exit_stack.enter_context(load_standin().GatewayStandIn(admin))
            return Stack(admin, standin, server, route_name)

        yield make
