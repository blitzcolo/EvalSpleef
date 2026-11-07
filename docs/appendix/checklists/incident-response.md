# 事故响应清单（隔离类）

> 状态：v1 · 维护：isolation · 最后更新：2026-11-06
> 上游：[04-isolation](../../04-isolation.md) §14 · 相关：[trial-hygiene](trial-hygiene.md) · [isolation-audit-report-template](isolation-audit-report-template.md)

## 用途

隔离类事故（跨 trial 泄漏、设备污染、槽位泄漏、密钥泄露、验证器被攻破、宿主被入侵）的现场处置单。**先取证、后清理**；所有事故建 `incident` 并关联 trial/host/device。等级：S1（跨 trial/密钥/宿主）2 h 初步结论、24 h 修复计划；S2（单设备污染/单题验证器）24 h；S3（warning）72 h。

## 0. 通用步骤

| 步骤 | 动作 | 负责人 | 时限 | 证据 |
| --- | --- | --- | --- | --- |
| D1 检测 | 确认信号来源（金丝雀/体检/监控/人工） | on-call | 立即 | 告警原文、时间线 |
| C1 遏制 | 冻结涉事 trial/宿主/设备；停止派发；必要时断网 | on-call | ≤15 min | 操作日志 |
| C2 取证 | 冻结现场：只读归档产物/轨迹/沙箱快照/网络日志；**不要先删除** | 取证负责人 | ≤30 min | 证据包 URI + sha256 |
| S1 定级 | 按影响面定 S1/S2/S3；通知 Lead | Lead | ≤30 min | incident 记录 |
| R1 影响面 | 列出涉事 trial、批次、已发布内容、同宿主/同设备时间窗重叠 trial | auditor | ≤2 h（S1） | 影响清单 |
| F1 修复 | 定位通道（C1–C13）→ 阻断 → 负向验证（构造一对测试 trial 复现并确认已阻断） | owner | 24 h（S1） | 修复 PR/配置 diff |
| R2 重跑 | 作废 trial 以新 index 重跑；重跑后再扫金丝雀 | scheduler | 按批次计划 | 新 trial 记录 |
| P1 复盘 | blameless 复盘：根因、漏检点、改进项、负责人与截止日 | Lead | 3 工作日内 | 复盘文档 |
| P2 披露 | S1/S2 进下期体检报告；已发布内容发更正说明 | Lead | 下期发布前 | 报告 + 公告 |

## 1. 跨 trial 泄漏（金丝雀 critical / 缓存 / 相似度）

- [ ] 立即冻结：命中方 trial + 令牌所属 trial + 同宿主时间窗重叠 trial 停止派发
- [ ] 保留：`isolation/canary/scan.json`、命中原文片段（脱敏）、产物/轨迹归档、沙箱快照
- [ ] 判定影响：按 `trials_to_invalidate` 双方作废；同宿主其他 trial 抽检
- [ ] 通道排查顺序：C1 文件系统 → C2 缓存 → C7 设备 → C9 预热池 → C4 网络 → C10 归档
- [ ] 负向验证：构造 A/B 两个测试 trial，A 写标记，B 必须读不到
- [ ] 修复 + 重跑 + 再扫描；更新体检报告

## 2. 设备污染

- [ ] 设备置 `quarantined`，禁止新租约
- [ ] 卸载非基线包、清临时目录、恢复出厂/清洁快照
- [ ] 核对上一个租约的 trial 与 `handover.json`（是否漏检、diff 是否为空）
- [ ] 受影响 trial invalid 重跑；相邻窗口 trial 抽检
- [ ] 更新归还清单或自动化脚本；连续 2 次 fail 的设备退出池

## 3. 槽位泄漏（macOS VM / GPU）

- [ ] 摘除异常宿主：停止派发、迁移排队 trial
- [ ] 检查残留：VM 进程、临时用户、GPU 进程、端口占用
- [ ] 重启宿主；槽位计数重新校准（每台物理 Mac 最多 2 个 macOS VM）
- [ ] 该宿主全部未完成 trial 迁移重跑；记录 incident
- [ ] 复核自动摘除策略（心跳 60 s、连续 2 次失联）

## 4. 密钥泄露

- [ ] **立即轮换**相关 key（模型/provider/对象存储/SSH）；吊销 per-trial 租约
- [ ] 封存沙箱与镜像，检查 env/文件/history 中密钥来源
- [ ] 检查网关用量与出站目标，判断是否有盗刷/外带
- [ ] 涉事 trial invalid；相关批次审计费用与网络日志
- [ ] 复核密钥平面（脑手分离、per-trial key、日志脱敏）；S1 事故

## 5. 验证器被攻破 / 测试泄露

- [ ] 题目立即下线；冻结该题相关 trial（未发布部分暂停）
- [ ] 比对测试文件哈希、镜像层、产物清单，定位泄漏点
- [ ] 受影响 trial 重判（regrade）或重跑（MAJOR 版本变更）
- [ ] 修复镜像构建/产物导出流程；重发题目版本；体检报告披露

## 6. 宿主被入侵（升级 S1）

- [ ] 宿主断网隔离；保留磁盘镜像与内存证据（如可行）
- [ ] 全面轮换该宿主接触过的凭证；检查其他宿主是否有相同 IOCs
- [ ] 该宿主全部 trial invalid；批次安全复核
- [ ] 基础设施团队复盘；恢复前重新做基线

## 7. 事故记录字段（写入 incident）

`incident_id`、`severity(S1/S2/S3)`、`type`、`detected_at`、`detected_by`、`status(open/contained/resolved)`、`affected_trials[]`、`affected_hosts[]`、`affected_devices[]`、`root_cause`、`channel(C1–C13)`、`actions_taken[]`、`evidence_uri`、`rerun_trials[]`、`published_disclosure`、`owner`、`review_due`、`closed_at`。

## 8. 通知矩阵

| 对象 | S1 | S2 | S3 |
| --- | --- | --- | --- |
| Lead / 项目负责人 | 立即 | 当日内 | 周报 |
| 基础设施/安全 | 立即 | 按需 | 不必 |
| 出题/判定负责人 | 当日内 | 当日内 | 周报 |
| 外部（发布更正） | Lead 决定 | 体检报告披露 | 不必 |
