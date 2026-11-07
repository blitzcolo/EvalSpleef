# debug-001 变异测试清单

> 目的：证明隐藏测试不是"只认参考解"，而是能抓住每一类缺陷。每次上线前跑
> bash hidden-tests/run.sh --selftest，其中包含变异测试步骤。

## 变异体

| 变异体 ID | 目标缺陷 | 注入方式（在已修复代码上） | 期望 | 实测 |
| --- | --- | --- | --- | --- |
| M1-truncate-rounding | D1 | money.round_money 改回 Decimal(int(float(x)*100))/100 | 隐藏测试失败 | killed |
| M2-inclusive-end | D2 | dates.in_range 的 value >= end 改为 value > end | 隐藏测试失败 | killed |
| M3-swallow-parse-error | D3 | load_rows 的 except 分支改为 continue | 隐藏测试失败 | killed |
| M4-mutable-default | D4 | group_by_month(rows, acc=None) 改回 acc={} | 隐藏测试失败 | killed |
| M5-mix-currencies | D5 | summarize 只保留单币种键并把各币种金额相加 | 隐藏测试失败 | killed |

## 运行方式

    # 1) 先把参考解 A 应用到 starter 的临时副本
    cp -R starter /tmp/fixed
    ( cd /tmp/fixed && patch -p1 < answers/reference-a.patch )
    # 2) 跑变异测试（期望 "OK：5/5 变异体被杀死"）
    python3 hidden-tests/mutate.py /tmp/fixed
    # 或者直接跑包含变异测试的完整自检
    bash hidden-tests/run.sh --selftest

## 变异得分

- 变异体总数：5
- 被杀死：5
- 存活：0
- 变异得分：100%

## 额外守护

- HG2 哈希锁本身也有"反变异"验证：selftest 会篡改 tests/test_public.py 后
  断言 check_locks.py 退出码为 3，防止锁形同虚设。
