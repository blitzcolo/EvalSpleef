"""并发采样语义： barrier 证明并发、结果按样本序号收集、每厂商并发上限。"""

from __future__ import annotations

import json
import threading
import time
import unittest

from evalspleef.core.judge import JudgeConfigError, JudgePanel
from evalspleef.core.models import CheckKind, RubricItem, Severity


def make_item(item_id: str = "R1") -> RubricItem:
    return RubricItem(
        id=item_id,
        severity=Severity.FUNCTIONAL,
        description="提交后出现结果面板",
        f1_symptom="点击提交后没有结果提示",
        judge_question="提交后是否出现结果面板",
        kind=CheckKind.LLM_JUDGE,
    )


class BarrierJudge:
    """每次调用都等在同一个 barrier 上：只有全部调用同时在飞才能通过。"""

    def __init__(self, barrier: threading.Barrier, name: str, family: str) -> None:
        self._barrier = barrier
        self.name = name
        self.provider = name
        self.family = family

    def complete(self, prompt: str) -> str:
        del prompt
        self._barrier.wait(timeout=15)
        return json.dumps({"verdict": True})


class TicketJudge:
    """按到达序号应答的客户端。

    到达序号即样本序号（面板按序提交任务、线程池按 FIFO 领取）。``reverse``
    时第 t 次到达的调用睡 ``1/(t+1)`` 秒，完成顺序严格倒序，从而把“按完成
    顺序收集结果”的错误实现暴露出来。应答 ``t > 0`` 非回文，倒序收集会
    得到不同的投票序列。
    """

    def __init__(self, name: str, family: str, *, reverse: bool = False) -> None:
        self.name = name
        self.provider = name
        self.family = family
        self._reverse = reverse
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0
        self.answers: list[bool] = []

    def complete(self, prompt: str) -> str:
        del prompt
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            ticket = len(self.answers)
            self.answers.append(ticket > 0)
        # 非 reverse 模式保留一个短暂持有时段，让并发上限的窗口可观测；
        # reverse 模式下第 t 次到达睡 1/(t+1) 秒，完成顺序严格倒序。
        time.sleep(1.0 / (ticket + 1) if self._reverse else 0.02)
        with self._lock:
            self._in_flight -= 1
        return json.dumps({"verdict": ticket > 0})


def make_panel(
    clients: dict[str, object],
    *,
    k: int = 5,
    max_parallel_per_vendor: int = 5,
) -> JudgePanel:
    return JudgePanel(
        clients,  # type: ignore[arg-type]
        target_model="target",
        target_family="target-family",
        k=k,
        max_parallel_per_vendor=max_parallel_per_vendor,
    )


class ConcurrentSamplingTest(unittest.TestCase):
    def test_all_samples_of_an_item_run_concurrently(self) -> None:
        vendors = 2
        k = 5
        barrier = threading.Barrier(vendors * k)
        clients = {
            "a": BarrierJudge(barrier, "judge-a", "family-a"),
            "b": BarrierJudge(barrier, "judge-b", "family-b"),
        }
        result = make_panel(clients, k=k).evaluate_item(make_item())
        self.assertTrue(result.passed)
        self.assertEqual(result.votes_by_vendor, {"a": (True,) * k, "b": (True,) * k})

    def test_votes_follow_sample_order_not_completion_order(self) -> None:
        # 完成顺序被压成到达顺序的严格倒序；若面板按完成顺序收票，votes 会是
        # answers 的倒序。断言二者一致，证明按提交序号收集。
        clients = {
            "a": TicketJudge("judge-a", "family-a", reverse=True),
            "b": TicketJudge("judge-b", "family-b", reverse=True),
        }
        result = make_panel(clients).evaluate_item(make_item())
        for vendor, client in clients.items():
            self.assertEqual(result.votes_by_vendor[vendor], tuple(client.answers))

    def test_cap_bounds_in_flight_calls_per_vendor(self) -> None:
        clients = {
            "a": TicketJudge("judge-a", "family-a"),
            "b": TicketJudge("judge-b", "family-b"),
        }
        result = make_panel(clients, k=5, max_parallel_per_vendor=2).evaluate_item(make_item())
        self.assertEqual(clients["a"].max_in_flight, 2)
        self.assertEqual(clients["b"].max_in_flight, 2)
        self.assertEqual(len(result.votes_by_vendor["a"]), 5)

    def test_invalid_cap_rejected(self) -> None:
        clients = {
            "a": TicketJudge("judge-a", "family-a"),
            "b": TicketJudge("judge-b", "family-b"),
        }
        with self.assertRaises(JudgeConfigError):
            make_panel(clients, max_parallel_per_vendor=0)

    def test_sample_exception_surfaces_as_before(self) -> None:
        class Boom:
            name = "judge-a"
            provider = "judge-a"
            family = "family-a"

            def complete(self, prompt: str) -> str:
                raise RuntimeError("boom")

        clients = {
            "a": Boom(),
            "b": TicketJudge("judge-b", "family-b"),
        }
        with self.assertRaisesRegex(RuntimeError, "boom"):
            make_panel(clients, k=2).evaluate_item(make_item())


if __name__ == "__main__":
    unittest.main()
