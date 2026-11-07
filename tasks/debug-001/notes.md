# debug-001 答案与验收说明

> 状态：v1 · 维护：tasks-b · 最后更新：2026-11-06
> 上游：[00-conventions](../../../docs/00-conventions.md) · 相关：[debug-repair playbook](../../playbooks/debug-repair.md)

## 1. 题包结构

    debug-001/
    ├── task.yaml / instruction.md / persona.md / rubric.yaml / defects.yaml
    ├── canary.md / notes.md / mutations.md
    ├── starter/                 # Agent 可见工作区（注入 5 处缺陷的工程）
    │   ├── README.md            # 行为契约（Agent 的唯一规格来源）
    │   ├── ledgerq/             # 实现（Python 标准库）
    │   ├── tests/test_public.py # 可见测试（哈希锁定，不得修改）
    │   └── sample/transactions.csv
    ├── answers/
    │   ├── reference-a.patch    # 最小改动参考解
    │   ├── reference-b.patch    # 重构式参考解（不同实现思路）
    │   └── empty-agent.md
    └── hidden-tests/
        ├── run.sh               # 隐藏验收入口（退出码 0=通过）
        ├── check_locks.py       # 测试文件哈希锁（硬门槛 HG2）
        ├── locks.sha256
        ├── mutate.py            # 变异测试（5/5 必杀死）
        ├── tests/test_regression.py   # 22 条隐藏回归测试
        └── fixtures/*.csv            # 隐藏数据集

## 2. 运行环境与依赖

- Python 3.11+，仅标准库；本地验证环境 python3 3.15.2 / macOS 27.2 arm64。
- 目标镜像：python:3.12-slim（digest 上线前固定；本机无需网络）。
- 无第三方包、无 pytest；测试用 unittest。

## 3. 验证命令

    # 隐藏验收（对候选工程；默认 starter，预期失败）
    bash hidden-tests/run.sh
    # 自检：参考解 A/B 通过、starter 失败、哈希锁生效、判分幂等、变异全杀
    bash hidden-tests/run.sh --selftest
    # 单独校验参考解
    cp -R starter /tmp/ref && ( cd /tmp/ref && patch -p1 < answers/reference-a.patch )
    bash hidden-tests/run.sh /tmp/ref

## 4. 实测证据（2026-11-06，本机）

    自检输出：
    debug-001 selftest
      [OK]   reference-a：隐藏测试通过（预期通过）
      [OK]   reference-b：隐藏测试通过（预期通过）
      [OK]   starter（未修复）：隐藏测试失败（预期失败）
      [OK]   tests/ 被修改：哈希锁拦截（预期拦截）
      [OK]   判分幂等：同一产物连跑两次均通过
      [OK]   [OK]   变异测试：5/5 变异体被隐藏测试杀死
    selftest: 6 通过 / 0 失败
    SELFTEST PASS

    starter 上直接跑隐藏测试：Ran 22 tests，FAILED (failures=15)
    参考解 A / B 上：Ran 22 tests，OK

## 5. 5 处缺陷与修复要点

| 缺陷 | 位置 | 契约条款 | 修复要点 |
| --- | --- | --- | --- |
| D1 | money.round_money | 第 1 条 HALF_UP | Decimal(str(x)).quantize(0.01, ROUND_HALF_UP)，不经 float |
| D2 | dates.in_range | 第 2 条 [start,end) | end 用 >= 排除 |
| D3 | pipeline.load_rows | 第 3 条 必须报错 | except 分支改 raise LedgerError(..., line_no) |
| D4 | pipeline.group_by_month | 第 8 条 API 行为 | 默认参数改 None，内部新建 dict |
| D5 | pipeline.summarize | 第 5 条 币种分开 | 按币种分桶求 Decimal 和，键排序后输出 |

## 6. 参考解差异

- reference-a.patch（85 行 diff）：只改 money/dates/pipeline 三个文件的对应函数，行数最小。
- reference-b.patch（213 行 diff）：改 csv.reader 逐列校验、date.fromisoformat、defaultdict 单遍聚合、
  localcontext 舍入；公开 API 与 CLI 不变，思路与 A 不同（解析/聚合/舍入三处都换了实现）。

## 7. 已知容差与限制

- 金额严格按 Decimal 精确比较，无容差（HALF_UP 结果确定）。
- 隐藏测试依赖文件路径 fixtures/，由 run.sh 注入 LEDGER_CANDIDATE 环境变量；不得在 Agent 镜像中出现。
- 哈希锁只覆盖 tests/ 下现存文件；Agent 新增测试文件不违规。
- canary 令牌不写入 tests/（否则会触碰哈希锁），见 canary.md。

## 8. Schema 校准

schemas/task.schema.json 与 schemas/rubric.schema.json 尚未落地；本包 YAML 按 Lead 2026-11-06
冻结字段（rubric 11 字段、budget 7+2 字段）与 00-conventions §3 编写。Schema 出现后由 tasks-b
逐字段校准，预计只影响字段名/层级，不影响可跑产物与 verdict 语义。

## 9. TODO

- 无 TODO(run-on-host)：本题全部验收脚本已在本地实跑通过。
- 上线前待办：固定 python:3.12-slim 镜像 digest；把 persona/canary 注入接入 Harbor 用户 agent。

## 10. 观测手段（给 Observer/裁判）

| 观察项 | 手段 | 命令 | 证据形式 |
| --- | --- | --- | --- |
| 隐藏测试结果 | 逐条 unittest 结果 | bash hidden-tests/run.sh -v | 通过/失败条目（F2） |
| 测试文件哈希 | sha256 清单比对 | python3 hidden-tests/check_locks.py | 退出码 + 差异清单 |
| 改动范围 | 与 starter 快照 diff | diff -ru starter candidate | 文件数/行数统计 |
| 变异测试 | 重新注入 5 处缺陷 | python3 hidden-tests/mutate.py <fixed> | killed/survived 表 |
| CLI 行为 | 端到端输出与退出码 | python3 -m ledgerq ...; echo $? | stdout/stderr/exit |
| 判分幂等 | 同一产物连跑两次 | bash hidden-tests/run.sh --selftest | 两次结果一致 |
