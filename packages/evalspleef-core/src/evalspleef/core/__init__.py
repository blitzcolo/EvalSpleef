"""evalspleef.core — LLM 自动化评测的 Python 标准库参考实现。

本包把 docs/00-conventions.md 第 4 节的数据契约与 docs/02-04 的流程/判定/
隔离规则实现为可 import、可测试、可 CLI 驱动的纯标准库代码，证明规格可执行。

模块地图
--------
- models: 枚举、档位映射、预算、rubric/defect/grade/trial 数据类；
- state: trial 状态机与重判 grade 版本；
- defects: 缺陷发现、去重、跨轮跟踪；
- feedback: F0-F3 反馈阶梯控制器与泄露检查；
- grading: 扣分、硬门槛短路、档位算法、裁判采样一致性；
- judge: 多厂商裁判面板、MockJudge、输出解析与重试；
- canary: 金丝雀令牌生成、埋点清单、跨 trial 扫描；
- report: 榜单单元格聚合与批次报告；
- schema_lite: 无第三方依赖的极简 JSON Schema 校验器；
- cli: python3 -m evalspleef.core 命令行入口。

约定：只用 Python 标准库；对外函数均带类型标注和中文 docstring。
"""

from .models import (
    Budget,
    BudgetUsage,
    CheckKind,
    Defect,
    DefectStatus,
    Disposition,
    ErrorClass,
    FeedbackLevel,
    Grade,
    RubricItem,
    Severity,
    Tier,
    TrialError,
    TrialRecord,
    TrialState,
    Validity,
)

__version__ = "0.1.0"

__all__ = [
    "Budget",
    "BudgetUsage",
    "CheckKind",
    "Defect",
    "DefectStatus",
    "Disposition",
    "ErrorClass",
    "FeedbackLevel",
    "Grade",
    "RubricItem",
    "Severity",
    "Tier",
    "TrialError",
    "TrialRecord",
    "TrialState",
    "Validity",
    "__version__",
]
