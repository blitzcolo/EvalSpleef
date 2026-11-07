"""Agent runner SPI (TECH-DESIGN section 8.1)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from .caps import ExecutionMode
from .model import WireProtocol
from .sandbox import Sandbox


class StopReason(StrEnum):
    """PRD appendix B.3."""

    COMPLETED = "completed"
    IDLE = "idle"
    NEED_CLARIFICATION = "need_clarification"
    TIMEOUT = "timeout"
    COST_CAP = "cost_cap"
    TOKEN_CAP = "token_cap"
    LOOP_DETECTED = "loop_detected"
    CRASH = "crash"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class RunnerBudget:
    """The subset of the trial budget a runner enforces locally; the gateway enforces the rest."""

    max_total_tokens: int
    max_cost_usd: float
    max_wall_clock_s: int


@dataclass(frozen=True)
class ShellTurn:
    """The outcome of one agent round."""

    stop_reason: StopReason
    final_message: str | None
    steps: int
    usage: TokenUsage
    cost_usd: float
    duration_s: float
    trace_ref: str | None = None


class AgentRunner(ABC):
    name: str
    """Shell slug from PRD appendix A.1, for example ``terminus`` or ``claude-code``."""
    version: str
    supported_modes: frozenset[ExecutionMode]
    required_protocol: WireProtocol

    @abstractmethod
    async def start(
        self,
        sandbox: Sandbox,
        *,
        gateway_url: str,
        trial_key: str,
        model_alias: str,
        instruction: str,
        budget: RunnerBudget,
    ) -> None: ...

    @abstractmethod
    async def send(self, message: str, *, deadline_s: int) -> ShellTurn: ...

    @abstractmethod
    async def stop(self) -> None:
        """Idempotent; kills the agent's process group."""
