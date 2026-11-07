# data-001 答案与验收说明

> 版本：1.0.0 · 维护：tasks-a · 最后更新：2026-11-06
> 相关：[docs/06-answer-and-acceptance.md](../../docs/06-answer-and-acceptance.md)

## 1. 题目形态

- 输入：混合 3 种格式 + 5 类脏行的访问日志；样例 50 行（含 harness 金丝雀注释行，按题面口径记为 `bad_format`）。
- 提交物：清洗脚本 `clean_logs.py`（CLI：输入日志 → 输出报告），验证侧在**样例**与**隐藏日志**上分别重跑，防硬编码。
- 隐藏日志由 `hidden-tests/gen_data.py`（seed 771001，425 行）在验证时生成；oracle 独立计算期望报告。
- 容差：计数精确；`error_rate` ±0.0001；`p95_duration_ms` ±1.0ms。

## 2. 文件与职责

| 文件 | 侧 | 职责 |
| --- | --- | --- |
| `task.yaml` | 题包 | 元数据、隔离声明、预算、硬门槛 |
| `instruction.md` | Agent | 格式/脏行/报告口径（含样例与容差） |
| `persona.md` | 用户 Agent | F0-F3 话术、FAQ、禁止泄露清单 |
| `rubric.yaml` | 判定 | 12 条原子 rubric |
| `defects.yaml` | 判定 | 覆盖 R1-R7、R9-R11 的现象素材 |
| `workspace/access.log` | Agent | 样例日志（哈希锁） |
| `workspace/expected_sample.json` | Agent | 样例正确报告（哈希锁，用于自检） |
| `answers/reference-a.py` | 验证 | pandas 向量化参考解（TODO(run-on-host)：本机无 pandas） |
| `answers/reference-b.py` | 验证 | 纯 Python 流式参考解（可跑） |
| `answers/empty-agent.md` | 验证 | 空 Agent 预期 Failed |
| `hidden-tests/gen_data.py` | 验证 | 样例/隐藏日志生成器 |
| `hidden-tests/oracle.py` | 验证 | 期望报告唯一口径 |
| `hidden-tests/checker.py` | 验证 | 报告比对（容差 + 格式） |
| `hidden-tests/verify_submission.py` | 验证 | 三硬门槛提交验收 |
| `hidden-tests/test_hidden.py` | 验证 | 12 条隐藏测试 |
| `hidden-tests/hashes.json` | 验证 | 测试与输入哈希锁 |

## 3. 复现命令（本地已实跑）

```bash
cd tasks/data-001
python3 answers/reference-b.py workspace/access.log /tmp/report.json
python3 hidden-tests/checker.py --answer /tmp/report.json
bash hidden-tests/run.sh
SUBMISSION=answers/reference-b.py bash hidden-tests/run.sh   # 走一遍提交验收
```

实跑证据（2026-11-06）：`bash hidden-tests/run.sh` → `Ran 12 tests ... OK (skipped=1)`（pandas 参考解在本机只做 py_compile，跳过运行）；参考解 B 在样例与隐藏数据上均与 oracle 完全一致。

## 4. 容差与依据

| 项 | 容差 | 理由 |
| --- | --- | --- |
| total_valid / malformed / unique_clients / malformed_by_reason / top_paths | 精确 | 整数计数，无测量误差 |
| error_rate | ±0.0001 | 4 位小数舍入本身的最大误差 5e-5，留一倍余量 |
| p95_duration_ms | ±1.0 ms | 允许浮点换算（ms/s）与舍入的差异；`nearest-rank` 口径必须一致 |

## 5. 变异测试

见 [mutations.md](mutations.md)：12 个一阶变异体全部被隐藏测试杀死，另有容差边界用例（0.00005 通过 / 0.0002 失败；+0.5ms 通过 / +2.0ms 失败）。

## 6. 已知限制

- pandas 参考解在开发机不可运行（无 pandas），只做编译校验；镜像内需用 `TODO(run-on-host)` 跑一次并回填证据。
- 隐藏日志由生成器确定性生成，不是真实流量；分布覆盖 5 类脏行与 3 种格式，但不对应真实业务占比。
- 若要做到「每月可复用」的更强验收，可加 MINOR 版本：让脚本支持 `--json` 之外的时间窗参数；当前版本不做，避免指令过长。

## 7. 版本

- 1.0.0（2026-11-06）：首版。改报告口径/容差 = MAJOR 重跑；改 rubric = MINOR 重判；改题面文案 = PATCH。