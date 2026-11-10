"""The agent judge's settings from the validated ``agent_judge`` section of batch.yaml."""

from __future__ import annotations

from pathlib import Path

from ..config.batch import AgentJudgeConfig
from .agent_judge import AgentJudgeSettings
from .agent_prompt import AgentJudgeLimits

__all__ = ["agent_judge_settings"]


def agent_judge_settings(
    config: AgentJudgeConfig, target_family: str, transcript_root: Path | None
) -> AgentJudgeSettings:
    """The step settings for a validated ``agent_judge`` section of batch.yaml."""
    limits = AgentJudgeLimits(
        max_steps=config.max_steps,
        max_invalid_replies=config.max_invalid_replies,
        command_timeout_s=config.command_timeout_s,
        wall_clock_s=config.wall_clock_s,
        output_cap_bytes=config.output_cap_bytes,
    )
    return AgentJudgeSettings(
        target_family=target_family,
        k=config.k,
        limits=limits,
        run_every_round=config.run_every_round,
        transcript_root=transcript_root,
    )
