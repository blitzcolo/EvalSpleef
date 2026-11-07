# 隔离体检报告模板（每期）

> 状态：模板 v1 · 维护：isolation · 最后更新：2026-11-06
> 上游：[04-isolation](../../04-isolation.md) §13 · 相关：[isolation-preflight](isolation-preflight.md) · [trial-hygiene](trial-hygiene.md) · [incident-response](incident-response.md)

> 使用方式：复制本文件为 `reports/<batch_id>-isolation-audit.md`，逐项填写并附证据 URI；`verdict` 必须为 **PASS** 才能发布批次。整份报告随批次归档，保留期与判分证据一致。

# <batch_id> 隔离体检报告

## 0. 元信息

| 字段 | 值 |
| --- | --- |
| batch_id | `2026-11-b1` |
| 报告版本 | `report_version: 1` |
| 体检周期 | `2026-11-01 ~ 2026-11-08` |
| 体检人（auditor） |  |
| 复核人（Lead） |  |
| 批次状态 | Frozen / Running / Graded / Audited / Published |
| 报告时间 |  |
| 证据根 URI | `s3://.../batches/<batch_id>/audit/` |
| 适用文档版本 | docs/04-isolation.md v1 |

## 1. 范围

| 项 | 数值 |
| --- | --- |
| 模型数 |  |
| 题目数 |  |
| trial 总数 / 有效 / 作废 |  |
| 平台分布 | linux-container / tart-macos / harmony-device-pool / gpu-node / bare-metal-perf |
| epoch 数 |  |
| 涉及宿主/设备 |  |

## 2. 期前检查（preflight）

| # | 检查项 | 结论（PASS/FAIL/WAIVER） | 证据 URI | 备注 |
| --- | --- | --- | --- | --- |
| P1 | Harbor lint 全绿（`--extend-select ALL`） |  |  |  |
| P2 | 镜像 digest 全固定 |  |  |  |
| P3 | 题包显式 separate + 非 root + network |  |  |  |
| P4 | 题包 canary/artifacts/network 声明齐全 |  |  |  |
| P5 | 网络策略冻结 + policy_sha256 |  |  |  |
| P6 | 宿主/设备健康 + 槽位无泄漏 |  |  |  |
| P7 | 预热池只含新实例 |  |  |  |
| P8 | 密钥平面与扫描工具就绪 |  |  |  |

## 3. 不变量检查（I1–I16）

| # | 不变量 | 通过 | 违反数 | 备注/证据 |
| --- | --- | --- | --- | --- |
| I1 | 沙箱/设备租约独占 |  |  |  |
| I2 | 根文件系统只读 |  |  |  |
| I3 | /tmp、$HOME、工作区私有 |  |  |  |
| I4 | 私有 netns + 默认断网 |  |  |  |
| I5 | 无宿主可写挂载/无 docker socket/无特权 |  |  |  |
| I6 | 显式非 root + 无 sudo |  |  |  |
| I7 | 资源配额 |  |  |  |
| I8 | 身份去标识 |  |  |  |
| I9 | 镜像 digest + 金丝雀注入 + 无凭证 |  |  |  |
| I10 | 验证沙箱独立 + 隐藏测试只在验证侧 |  |  |  |
| I11 | 产物净化 |  |  |  |
| I12 | 测试哈希锁 + 清空旧 reward |  |  |  |
| I13 | 金丝雀跨 trial 零 critical |  |  |  |
| I14 | 设备/宿主归还卫生 + 不回池 |  |  |  |
| I15 | 裁判/模拟用户上下文隔离 |  |  |  |
| I16 | 网络留痕完整 + 绕过可判定 |  |  |  |

**豁免（waiver）**：仅允许非安全类、且有 Lead 书面批准；安全类（I1–I6、I9、I13）不允许豁免。

| # | 不变量 | 原因 | 批准人 | 截止日期 | 补偿措施 |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

## 4. 金丝雀扫描

| 项 | 值 |
| --- | --- |
| 扫描 trial 数 / 令牌数 |  |
| critical 命中 | 0（必须为 0） |
| warning 命中（unknown_token/missing_placement） |  |
| 涉事 trial → 作废清单 |  |
| 扫描器版本 / 报告 URI |  |
| 结论 | PASS / FAIL |

## 5. 泄露通道抽查（13 类）

| # | 通道 | 抽查样本数 | 发现问题 | 处置 |
| --- | --- | --- | --- | --- |
| C1 | 文件系统 |  |  |  |
| C2 | 构建/包缓存 |  |  |  |
| C3 | Agent 记忆与会话 |  |  |  |
| C4 | 本机服务与端口 |  |  |  |
| C5 | 公网与发布通道 |  |  |  |
| C6 | 模型服务端状态 |  |  |  |
| C7 | 共享设备 |  |  |  |
| C8 | 裁判上下文 |  |  |  |
| C9 | 预热池/快照 |  |  |  |
| C10 | 日志/轨迹/产物归档 |  |  |  |
| C11 | 控制面与密钥 |  |  |  |
| C12 | 人工与外部观察者 |  |  |  |
| C13 | 时间与侧信道 |  |  |  |

## 6. 产物与验证沙箱

| 项 | 值 |
| --- | --- |
| 抽查 trial 数（每平台 ≥3） |  |
| 产物净化违规 |  |
| 超大小/数量限制 |  |
| 验证前 reward 残留 |  |
| 测试哈希不一致 |  |
| regrade 可用性（result.json + artifacts manifest） |  |

## 7. 设备与宿主

| 项 | 值 |
| --- | --- |
| 检查设备/宿主数 |  |
| 归还 pass / fail |  |
| 非基线包残留（added/removed 合计） |  |
| 基线哈希不一致 |  |
| 槽位泄漏事件 |  |
| 摘除/隔离的宿主或设备 |  |

## 8. 作弊审计

| 项 | 值 |
| --- | --- |
| 审计方式 | harbor analyze（100% trial）+ 相似度检测（winnowing/MOSS） |
| 审计 trial 数 |  |
| 自动标记疑似 |  |
| 人工确认作弊（G-CHEAT-01） |  |
| 疑似待仲裁（G-ANOMALY-98） |  |
| 相似度异常（同题跨模型 Jaccard ≥0.7 / 同模型跨 epoch ≥0.9） |  |
| 处置执行情况 |  |

## 9. 密钥与日志

| 项 | 值 |
| --- | --- |
| 密钥扫描覆盖 trial 数 |  |
| 沙箱内密钥命中 | 0（必须为 0） |
| 日志脱敏抽检命中 |  |
| 网关用量异常 |  |
| key 轮换事件 |  |

## 10. 事故

| incident_id | 等级 | 类型 | 关联 trial/host/device | 状态 | 证据 URI |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

## 11. 结论

| 项 | 值 |
| --- | --- |
| verdict | **PASS / CONDITIONAL / FAIL** |
| 阻断发布的问题 |  |
| 未关闭的 S1/S2 事故 |  |
| 允许发布的范围（CONDITIONAL 时） |  |
| 下一期必须完成的改进 |  |

## 12. 签字

| 角色 | 姓名 | 结论 | 时间 |
| --- | --- | --- | --- |
| 体检人（auditor） |  |  |  |
| Lead |  |  |  |
| 安全/基础设施（如涉及 S1） |  |  |  |

## 13. 附件

- [ ] `isolation/` 证据包索引（批内每个 trial）
- [ ] 金丝雀扫描报告 `canary-report.md`
- [ ] 产物净化抽检记录
- [ ] 设备归还记录汇总
- [ ] 作弊审计报告（含 `harbor analyze` 输出）
- [ ] 相似度报告
- [ ] 事故单与复盘链接
