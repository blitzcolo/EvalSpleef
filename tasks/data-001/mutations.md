# data-001 变异测试清单

要求 ≥5 个变异体被杀死（[docs/06-answer-and-acceptance.md](../../../docs/06-answer-and-acceptance.md) 第 5 节）。执行：`bash hidden-tests/run.sh` 中 `test_t05_mutants_killed` 与 `test_t07_tolerance_boundaries`。

| 编号 | 变异操作 | 期望被杀 | 杀死断言 |
| --- | --- | --- | --- |
| M1 | total_valid +1 | 是 | VALUE_MISMATCH |
| M2 | unique_clients -1 | 是 | VALUE_MISMATCH |
| M3 | malformed +2 | 是 | VALUE_MISMATCH |
| M4 | malformed_by_reason.bad_format +1 | 是 | VALUE_MISMATCH |
| M5 | error_rate +0.001 | 是 | 超出 ±0.0001 容差 |
| M6 | p95_duration_ms +5.0 | 是 | 超出 ±1.0ms 容差 |
| M7 | top_paths 逆序 | 是 | TOP_PATHS_NOT_SORTED |
| M8 | top_paths[0].count +1 | 是 | VALUE_MISMATCH |
| M9 | 顶层加 notes 键 | 是 | TOP_LEVEL_KEYS |
| M10 | 删除 p95_duration_ms | 是 | TOP_LEVEL_KEYS |
| M11 | total_valid 改成字符串 | 是 | COUNT_TYPE |
| M12 | 外层包一段说明文字 | 是 | NOT_PURE_JSON |
| M13 | 空报告 / 不存在文件 | 是 | REPORT_MISSING |
| M14 | 容差边界：error_rate +5e-5（通过）/ +2e-4（失败） | 是（反向用例也断言） | test_t07 |
| M15 | 容差边界：p95 +0.5ms（通过）/ +2.0ms（失败） | 是 | test_t07 |

## 变异分数

- 一阶变异体 13 个：杀死率 13/13；容差边界 2 组双向断言通过。
- 报告口径本身被改（如换成 floor 分位）无法被上述变异体发现，靠 R6 裁判条目 + 人工复核。