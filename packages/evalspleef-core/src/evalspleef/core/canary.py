"""evalspleef.core.canary — 金丝雀令牌生成、埋点清单与跨 trial 扫描。

事实来源：
- docs/00-conventions.md §2：令牌格式 CNRY-{trial_id 短哈希 8 位}-{随机 8 位}；
- drafts/plan-v1.md §5.3：每个 trial 唯一令牌，写进题目文件注释、示例数据与一个
  不起眼的文件；全部 trial 结束后扫描轨迹/产物/网络日志，出现别的 trial 的令牌
  即判定隔离失效，相关 trial 作废重跑；
- docs/04-isolation.md（isolation 队友维护，落地后逐条校准违规分类与处置）。

扫描三类位置：产物（artifact）、轨迹（trace）、网络日志（network）。
violation kind：
- cross_trial      本 trial 的内容里出现别的 trial 的令牌（隔离失效，作废重跑）
- egress           自己的令牌出现在白名单外主机的网络日志里（外泄，作废重跑）
- missing_placement 声明的埋点位置缺失（埋点完整性告警）
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CANARY_TOKEN_PATTERN",
    "CanaryPlacement",
    "CanaryRegistry",
    "CanaryReport",
    "CanaryTrial",
    "CanaryViolation",
    "NetworkEntry",
    "build_placements",
    "generate_token",
    "inject_comment",
    "scan_trials",
    "short_hash",
    "validate_token",
]

CANARY_TOKEN_PATTERN = re.compile(r"^CNRY-[0-9a-f]{8}-[0-9a-f]{8}$")
_TOKEN_IN_TEXT = re.compile(r"CNRY-[0-9a-f]{8}-[0-9a-f]{8}")

#: 违规 kind -> 严重度；critical 的 trial 必须作废重跑（docs/02 §6 P0）。
VIOLATION_SEVERITY: dict[str, str] = {
    "cross_trial": "critical",
    "egress": "critical",
    "missing_placement": "warning",
    "unknown_token": "warning",
}


def short_hash(trial_id: str) -> str:
    """trial_id 的 SHA-256 前 8 位十六进制（00-conventions §2）。"""
    return hashlib.sha256(trial_id.encode("utf-8")).hexdigest()[:8]


def generate_token(trial_id: str, *, nonce: str | None = None) -> str:
    """生成 trial 金丝雀令牌：CNRY-{trial_id 短哈希}-{随机 8 位}。

    Args:
        trial_id: 规范 trial ID。
        nonce: 8 位十六进制随机串；None 时用 secrets 生成（测试可注入固定值）。
    """
    if not trial_id:
        raise ValueError("trial_id 不能为空")
    if nonce is None:
        nonce = secrets.token_hex(4)
    if not re.fullmatch(r"[0-9a-f]{8}", nonce):
        raise ValueError(f"nonce 必须是 8 位小写十六进制，收到: {nonce!r}")
    token = f"CNRY-{short_hash(trial_id)}-{nonce}"
    validate_token(token)
    return token


def validate_token(token: str) -> str:
    """校验令牌格式；不合法抛 ValueError。"""
    if not CANARY_TOKEN_PATTERN.match(token):
        raise ValueError(f"金丝雀令牌不合法，期望 CNRY-{'{8 hex}'}-{'{8 hex}'}，收到: {token!r}")
    return token


@dataclass(frozen=True)
class CanaryPlacement:
    """一处埋点声明：把令牌写进哪个文件/哪类位置。"""

    trial_id: str
    path: str
    kind: str = "file"
    token: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("file", "comment", "sample_data", "env", "workspace"):
            raise ValueError(f"未知埋点类型: {self.kind!r}")
        if self.token:
            validate_token(self.token)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "trial_id": self.trial_id,
            "path": self.path,
            "kind": self.kind,
            "token": self.token,
        }


def build_placements(
    trial_id: str,
    token: str,
    paths: Sequence[str],
    *,
    kind: str = "comment",
) -> tuple[CanaryPlacement, ...]:
    """按题目 canary.md 声明的路径清单生成埋点清单。"""
    validate_token(token)
    return tuple(
        CanaryPlacement(trial_id=trial_id, path=path, kind=kind, token=token) for path in paths
    )


def inject_comment(content: str, token: str, *, style: str = "#") -> str:
    """把令牌注入到文件内容顶部的注释行（文档/脚本/配置通用）。"""
    validate_token(token)
    return f"{style} canary: {token}\n{content}"


@dataclass(frozen=True)
class NetworkEntry:
    """一条网络日志（URL + 可选请求体摘要）。"""

    host: str
    url: str = ""
    body: str = ""

    def text(self) -> str:
        """合并后用于令牌扫描的文本。"""
        return f"{self.url}\n{self.body}"

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {"host": self.host, "url": self.url, "body": self.body}


@dataclass
class CanaryTrial:
    """一个 trial 参与扫描的数据：令牌 + 产物 + 轨迹 + 网络日志 + 埋点。"""

    trial_id: str
    token: str
    artifacts: dict[str, str] = field(default_factory=dict)
    trace: list[str] = field(default_factory=list)
    network: list[NetworkEntry] = field(default_factory=list)
    expected_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_token(self.token)
        if not self.trial_id:
            raise ValueError("trial_id 不能为空")

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "trial_id": self.trial_id,
            "token": self.token,
            "artifacts": dict(self.artifacts),
            "trace": list(self.trace),
            "network": [entry.to_dict() for entry in self.network],
            "expected_paths": list(self.expected_paths),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CanaryTrial:
        """从 JSON 构造。"""
        return cls(
            trial_id=str(data["trial_id"]),
            token=str(data["token"]),
            artifacts=dict(data.get("artifacts", {})),
            trace=list(data.get("trace", [])),
            network=[NetworkEntry(**entry) for entry in data.get("network", [])],
            expected_paths=tuple(data.get("expected_paths", ())),
        )


@dataclass(frozen=True)
class CanaryViolation:
    """一次扫描违规。"""

    kind: str
    trial_id: str
    token: str
    location: str
    severity: str = "critical"
    token_owner: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "kind": self.kind,
            "trial_id": self.trial_id,
            "token": self.token,
            "token_owner": self.token_owner,
            "location": self.location,
            "severity": self.severity,
            "detail": self.detail,
        }


@dataclass
class CanaryReport:
    """跨 trial 扫描报告。"""

    violations: tuple[CanaryViolation, ...] = ()
    scanned_trials: tuple[str, ...] = ()
    tokens_seen: int = 0

    @property
    def critical(self) -> tuple[CanaryViolation, ...]:
        """critical 违规（必须作废重跑的隔离失效）。"""
        return tuple(item for item in self.violations if item.severity == "critical")

    @property
    def trials_to_invalidate(self) -> tuple[str, ...]:
        """需要作废的 trial 集合（去重排序）。

        涉事双方都作废：出现别的 trial 令牌的一方，以及令牌被别处读到的一方。
        """
        affected: set[str] = set()
        for violation in self.violations:
            if violation.severity != "critical":
                continue
            affected.add(violation.trial_id)
            if violation.token_owner:
                affected.add(violation.token_owner)
        return tuple(sorted(affected))

    @property
    def passed(self) -> bool:
        """是否零 critical 违规（隔离体检通过）。"""
        return not self.critical

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 的 dict。"""
        return {
            "passed": self.passed,
            "scanned_trials": list(self.scanned_trials),
            "tokens_seen": self.tokens_seen,
            "violations": [item.to_dict() for item in self.violations],
            "critical_count": len(self.critical),
            "trials_to_invalidate": list(self.trials_to_invalidate),
        }

    def to_markdown(self) -> str:
        """渲染成隔离体检报告（批次附录可直接贴）。"""
        lines = [
            "### 金丝雀跨 trial 扫描",
            "",
            f"- 扫描 trial 数：{len(self.scanned_trials)}",
            f"- 出现令牌次数：{self.tokens_seen}",
            f"- 结论：{'通过' if self.passed else '未通过（涉事 trial 作废重跑）'}",
        ]
        if self.violations:
            lines.extend(
                [
                    "",
                    "| kind | trial | token | 位置 | 严重度 | 说明 |",
                    "| --- | --- | --- | --- | --- | --- |",
                ]
            )
            for violation in self.violations:
                lines.append(
                    f"| {violation.kind} | {violation.trial_id} | {violation.token} | "
                    f"{violation.location} | {violation.severity} | {violation.detail} |"
                )
        return "\n".join(lines)


class CanaryRegistry:
    """trial -> 令牌登记表（由调度器在租约成功时写入）。"""

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    def register(self, trial_id: str, token: str | None = None) -> str:
        """为一个 trial 登记令牌；已存在时校验一致后返回旧值。"""
        if trial_id in self._tokens:
            existing = self._tokens[trial_id]
            if token is not None and token != existing:
                raise ValueError(f"trial {trial_id} 已有令牌 {existing}，不能再登记 {token}")
            return existing
        token = token or generate_token(trial_id)
        validate_token(token)
        if token in self._tokens.values():
            raise ValueError(f"令牌重复（每个 trial 必须唯一）: {token}")
        self._tokens[trial_id] = token
        return token

    def token_of(self, trial_id: str) -> str:
        """取令牌；未登记抛 KeyError。"""
        return self._tokens[trial_id]

    def by_token(self) -> dict[str, str]:
        """token -> trial_id 的反向索引。"""
        return {token: trial_id for trial_id, token in self._tokens.items()}

    def to_dict(self) -> dict[str, str]:
        """序列化为可 JSON 的 dict。"""
        return dict(sorted(self._tokens.items()))

    @classmethod
    def from_dict(cls, data: Mapping[str, str]) -> CanaryRegistry:
        """从 JSON 构造并校验格式/唯一性。"""
        registry = cls()
        for trial_id, token in data.items():
            registry.register(str(trial_id), str(token))
        return registry


def _scan_text(
    *,
    text: str,
    source_trial: str,
    location: str,
    token_owner: Mapping[str, str],
    allowlisted_hosts: frozenset[str],
    host: str | None,
) -> tuple[list[CanaryViolation], int]:
    """扫描一段文本，返回 (违规列表, 命中令牌数)。"""
    violations: list[CanaryViolation] = []
    seen: set[tuple[str, str]] = set()
    count = 0
    for match in _TOKEN_IN_TEXT.finditer(text):
        token = match.group(0)
        count += 1
        owner = token_owner.get(token)
        if owner is not None and owner != source_trial:
            key = ("cross_trial", token)
            if key not in seen:
                seen.add(key)
                violations.append(
                    CanaryViolation(
                        kind="cross_trial",
                        trial_id=source_trial,
                        token=token,
                        token_owner=owner,
                        location=location,
                        severity="critical",
                        detail=f"trial {source_trial} 的内容中出现 trial {owner} 的令牌",
                    )
                )
            continue
        if owner is None and host is not None:
            key = ("unknown_token", token)
            if key not in seen:
                seen.add(key)
                violations.append(
                    CanaryViolation(
                        kind="unknown_token",
                        trial_id=source_trial,
                        token=token,
                        location=location,
                        severity="warning",
                        detail="出现未登记的 CNRY 令牌",
                    )
                )
            continue
        if host is not None and host not in allowlisted_hosts:
            key = ("egress", token)
            if key not in seen:
                seen.add(key)
                violations.append(
                    CanaryViolation(
                        kind="egress",
                        trial_id=source_trial,
                        token=token,
                        token_owner=owner,
                        location=location,
                        severity="critical",
                        detail=f"自身令牌出现在白名单外主机 {host}",
                    )
                )
    return violations, count


def scan_trials(
    trials: Iterable[CanaryTrial],
    *,
    token_owner: Mapping[str, str] | None = None,
    allowed_hosts: Iterable[str] = (),
) -> CanaryReport:
    """跨 trial 扫描产物/轨迹/网络日志，返回隔离体检报告。

    Args:
        trials: 参与扫描的 trial 数据。
        token_owner: token -> trial_id；缺省从 trials 的 token 字段推导。
        allowed_hosts: 允许出现的网络主机白名单（docs/04 的网络策略）。
    """
    trial_list = list(trials)
    if token_owner is None:
        token_owner = {trial.token: trial.trial_id for trial in trial_list}
    token_owner = dict(token_owner)
    allow = frozenset(allowed_hosts)
    violations: list[CanaryViolation] = []
    witnessed = 0
    for trial in trial_list:
        for path, content in sorted(trial.artifacts.items()):
            found, count = _scan_text(
                text=content,
                source_trial=trial.trial_id,
                location=f"artifact:{path}",
                token_owner=token_owner,
                allowlisted_hosts=allow,
                host=None,
            )
            violations.extend(found)
            witnessed += count
        for index, trace_line in enumerate(trial.trace):
            found, count = _scan_text(
                text=trace_line,
                source_trial=trial.trial_id,
                location=f"trace:{index}",
                token_owner=token_owner,
                allowlisted_hosts=allow,
                host=None,
            )
            violations.extend(found)
            witnessed += count
        for entry in trial.network:
            found, count = _scan_text(
                text=entry.text(),
                source_trial=trial.trial_id,
                location=f"network:{entry.host}",
                token_owner=token_owner,
                allowlisted_hosts=allow,
                host=entry.host,
            )
            violations.extend(found)
            witnessed += count
        for path in trial.expected_paths:
            if path not in trial.artifacts:
                violations.append(
                    CanaryViolation(
                        kind="missing_placement",
                        trial_id=trial.trial_id,
                        token=trial.token,
                        token_owner=trial.trial_id,
                        location=f"artifact:{path}",
                        severity="warning",
                        detail="声明的金丝雀埋点文件缺失",
                    )
                )
    return CanaryReport(
        violations=tuple(violations),
        scanned_trials=tuple(trial.trial_id for trial in trial_list),
        tokens_seen=witnessed,
    )


def scan_documents(
    trials: Iterable[Mapping[str, Any]],
    *,
    token_owner: Mapping[str, str] | None = None,
    allowed_hosts: Iterable[str] = (),
) -> CanaryReport:
    """从 JSON 文档（或已构造的 CanaryTrial）扫描（CLI 入口用）。"""
    parsed: list[CanaryTrial] = []
    for item in trials:
        if isinstance(item, CanaryTrial):
            parsed.append(item)
        else:
            parsed.append(CanaryTrial.from_dict(dict(item)))
    return scan_trials(parsed, token_owner=token_owner, allowed_hosts=allowed_hosts)
