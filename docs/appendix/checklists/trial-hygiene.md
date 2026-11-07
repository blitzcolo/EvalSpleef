# Trial 卫生清单（每个 trial）

> 状态：v1 · 维护：isolation · 最后更新：2026-11-06
> 上游：[04-isolation](../../04-isolation.md) · 相关：[isolation-preflight](isolation-preflight.md) · [device-pool-handover](device-pool-handover.md) · [incident-response](incident-response.md)

## 用途

一个 trial 从 `leased` 到 `destroyed` 的全过程按本清单逐项打勾；每项都要留下证据 URI（写进 `trial.isolation` 或事件流）。任何一项失败按「失败处置」列执行，不得跳过。

## 1. 启动阶段（leased → running）

| # | 动作 | 命令/证据 | 失败处置 |
| --- | --- | --- | --- |
| 1.1 | 租约与实例 | provider 回执；`lease_id`；`from_warm_pool` | 换宿主重试 |
| 1.2 | 生成金丝雀令牌 | `evalkit.canary.generate_token(trial_id)` → `CNRY-...` | 重试；仍失败 → invalid |
| 1.3 | 按 `canary.md` 注入全部埋点 | 每个位置的 sha256 写入 `placements[]` | 缺一 → invalid |
| 1.4 | 身份去标识 | 随机工作区（如 `/work/w8f3a91`）；中性用户名/主机名；清空历史 `$HOME` 模板 | 修 provisioner 后重建 |
| 1.5 | 预检 I1–I9 | 见 [isolation-preflight](isolation-preflight.md) §B | 任一 fail → 按预检清单处置 |
| 1.6 | 启动外壳（沙箱外） | `ssh_control_plane_only=true`；工具调用经 SSH/ACP | 失败按环境错误重试 |
| 1.7 | 密钥扫描 | 扫描 env/文件/history：`grep -rE 'sk-|BEGIN (RSA|OPENSSH) PRIVATE KEY'` | 命中 → invalid + 轮换 key + S1 事故 |
| 1.8 | 网络策略生效确认 | `network_mode` 与策略哈希；出口代理日志开始记录 | 立即停止派发 |

## 2. 运行阶段（running / observing / awaiting_feedback）

| # | 动作 | 检查点 | 失败处置 |
| --- | --- | --- | --- |
| 2.1 | 心跳 | 外壳每 15 s 上报；3 次缺失判挂起 | 杀进程组 → observing |
| 2.2 | 预算 | 网关实时计费；token/墙钟/费用 达上限即停 | `stop_reason=cost_cap/token_cap/timeout` |
| 2.3 | 网络留痕 | 每轮结束核对 `blocked_requests`、`bypass_attempts` | 绕过 >0 → 人工确认 |
| 2.4 | 金丝雀自检 | 本轮轨迹/产物里不出现**其他** trial 令牌（批次末统一扫描，运行中先抽检） | 命中 → 立即停 trial + S1 |
| 2.5 | 反馈泄露检查 | 每条反馈 `leak_check.final_action ∈ {send, blocked}`；blocked 不发送 | 3 次重写失败 → feedback_blocked |
| 2.6 | 观察器只读 | 观察器不写工作区；不在沙箱内跑隐藏测试 | 违反 → 观察结果作废 |
| 2.7 | 设备独占 | 设备租约 `in_use` 且无其他 trial | 冲突 → 停用设备 |
| 2.8 | 密钥不回显 | 轨迹/日志出现密钥模式即脱敏并告警 | 命中 → S1 |

## 3. 验证阶段（verifying）

| # | 动作 | 检查点 | 失败处置 |
| --- | --- | --- | --- |
| 3.1 | 产物清单比对 | 只导出 `artifacts[]` 声明路径；数量/大小上限 | 未声明产物不导出 |
| 3.2 | 净化 | 丢弃软链/FIFO/socket/设备/setuid；权限归一 | 丢弃并记 warning；夹带 → cheat 线索 |
| 3.3 | 哈希 | 每产物 sha256；与清单复算一致 | 不一致 → 重新导出 |
| 3.4 | 新验证沙箱 | 独立 digest/租约、网络 off | 起不来 → failed_infra |
| 3.5 | 清空旧 reward | `/logs/verifier/reward.txt`、`reward.json` 均不存在 | 存在 → 可疑，按 cheat 排查 |
| 3.6 | 测试哈希锁 | `sha256sum -c tests.sha256 --quiet` | 不一致 → cheat（G-CHEAT-01） |
| 3.7 | 隐藏测试 | `hidden-tests/run.sh` 退出码 + 结构化结果；性能类 5 次取中位 | 超时/偶发失败 → regrade，不稳定则题目下线 |
| 3.8 | 裁判隔离 | 每 trial 独立会话；输入快照 sha256；无其他 trial 文本 | 串台 → invalid + 重判 |
| 3.9 | grade 版本 | `trial.result.grades[]` 追加版本；invalid 不写 grade | 违反 → 记录修复 |

## 4. 结束阶段（scored/invalid → destroyed）

| # | 动作 | 检查点 | 失败处置 |
| --- | --- | --- | --- |
| 4.1 | 杀进程组 | 导出产物前结束全部后台进程 | 残留 → 强制结束 |
| 4.2 | 销毁实例 | 沙箱删除；**不得回预热池** | 误回池 → 池审计 + 销毁 |
| 4.3 | 记录销毁时间 | `timestamps.destroyed_at`、`sandbox.destroyed_at` | 缺失 → 体检 FAIL |
| 4.4 | 设备归还 | 按 [device-pool-handover](device-pool-handover.md) 执行 | 失败 → 设备 quarantined + incident |
| 4.5 | 金丝雀扫描入队 | 批次末交叉扫描；结果写 `scan_status` | 漏扫 → 体检 FAIL |
| 4.6 | 归档证据包 | `isolation/` 目录 + 事件流 + 轨迹 | 缺失 → 审计不可复核 |
| 4.7 | 事故登记 | 任何异常建 `incident` 并关联 trial | 漏记 → 复盘补记 |

## 5. 关键断言（可直接运行）

~~~bash
# 单 trial 卫生断言：字段完整 + 无泄漏信号
python3 - "$TRIAL_DIR/trial.json" <<'PY'
import json, sys
t = json.load(open(sys.argv[1]))
iso = t["isolation"]
assert iso["sandbox"]["root_readonly"] is True, "根不是只读"
assert iso["sandbox"]["non_root_user"] != "root", "未显式配置非 root"
assert iso["secrets"]["api_key_in_sandbox"] is False, "密钥进入沙箱"
assert iso["secrets"]["ssh_control_plane_only"] is True, "SSH 非单向"
assert iso["canary"]["scan_status"] != "critical_violation", "金丝雀 critical 命中"
if t["state"] == "scored":
    assert t["validity"] == "valid" and t["result"]["grades"], "scored 但无 grade"
    assert t["timestamps"]["destroyed_at"], "未销毁"
print("trial 卫生断言通过:", t["trial_id"])
PY
~~~

## 6. 签字

| 阶段 | 执行人 | 时间 | 结论（PASS/FAIL） | 证据 URI |
| --- | --- | --- | --- | --- |
| 启动 |  |  |  |  |
| 运行 |  |  |  |  |
| 验证 |  |  |  |  |
| 结束 |  |  |  |  |
