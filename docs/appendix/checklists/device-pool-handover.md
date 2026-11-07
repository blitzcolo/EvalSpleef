# 设备/宿主归还清单（每租约）

> 状态：v1 · 维护：isolation · 最后更新：2026-11-06
> 上游：[04-isolation](../../04-isolation.md) §11 · 相关：[trial-hygiene](trial-hygiene.md) · [incident-response](incident-response.md)

## 用途

鸿蒙真机、macOS 性能机、GPU 节点在**每个租约结束**后执行；不通过不得回到 `free`。结果写 `isolation/device/handover.json`（字段见 [trial.schema.json](../../../schemas/trial.schema.json) `$defs.handover_check`）。

## 1. 通用前置

- [ ] 租约状态为 `in_use → handover_check`，无其他 trial 排队占用
- [ ] 设备上所有测试 App/进程已停止；无遗留后台服务
- [ ] 采集「归还前」快照：包列表、关键配置、基线哈希

## 2. 鸿蒙设备池（L4）

| # | 步骤 | 命令 | 通过标准 |
| --- | --- | --- | --- |
| 1 | 确认在线 | `hdc list targets` | 设备 ID 在列表 |
| 2 | 停止应用 | `hdc shell aa force-stop <bundle>`（aa 为应用调试工具） | 无目标进程 |
| 3 | 卸载非基线包 | `hdc uninstall <bundle>`（逐个） | 只剩基线包 |
| 4 | 清临时目录 | `hdc shell rm -rf /data/local/tmp/*` | 目录无测试产物 |
| 5 | 采集包列表 | `hdc shell bm dump -a > packages-after.txt`（bm 为内置包管理模块；子命令以设备 `bm -h` 为准，TODO(run-on-host)） | 与基线 diff 为空 |
| 6 | 重启/恢复 | `hdc shell reboot`；必要时恢复出厂镜像 | 重启后在线 |
| 7 | 基线哈希 | 包列表 + 关键配置 → sha256 | 与登记基线一致 |
| 8 | 记录 | 写 `handover.json`：`status`、`package_diff`、`baseline_hash` | `status=pass` |

~~~bash
# 归还 diff 断言（控制面执行）
diff -u baseline-packages.txt packages-after.txt > package.diff || true
test ! -s package.diff || { echo "FAIL: 包列表不一致"; cat package.diff; exit 1; }
sha256sum packages-after.txt | awk '{print $1}' > package.hash
grep -q "$(cat package.hash)" baseline.hash || { echo "FAIL: 基线哈希不一致"; exit 1; }
echo "PASS: 设备可归还"
~~~

**不通过处置**：设备置 `quarantined`；检查上一个租约的 trial；无法恢复出厂 → 退出池并采购/送修；同时开 incident（设备污染）。

## 3. macOS 性能机（L4）

| # | 步骤 | 命令/操作 | 通过标准 |
| --- | --- | --- | --- |
| 1 | 停止 App | `osascript -e 'quit app "<App>"'` / `killall` | 无目标进程 |
| 2 | 删除临时用户 | 删除本次创建的测量用户及其 `~/Library`、缓存、签名产物 | 用户列表回到基线 |
| 3 | 清临时目录 | 清 `/tmp`、`~/Library/Developer/Xcode/DerivedData` | 无测试产物 |
| 4 | 恢复基线 | 恢复清洁快照/镜像；关闭自动更新与 Spotlight 索引（测量前已关闭的保持） | 基线指纹一致 |
| 5 | 采集 Applications | `system_profiler SPApplicationsDataType` 或 `ls /Applications` | 与基线 diff 为空 |
| 6 | 记录 | `handover.json` | `status=pass` |

**性能机专属**：测量互斥（每机 1 路）；每次测量记录温度/负载/电源模式；跑完必须删除临时用户，禁止留常驻服务或 Agent 进程。

## 4. GPU 节点（L1/L3）

- [ ] 容器销毁；显存/算力配额释放（`nvidia-smi` 无残留进程）
- [ ] 无遗留端口/服务；`docker ps` 无本 trial 容器
- [ ] 节点健康与温度正常；异常时摘除并通知基础设施团队
- [ ] 记录 `handover.json`（`pool=gpu-node`）

## 5. 预热池规则

- [ ] 跑过任何 trial 的实例**不得回预热池**；回池实例必须是新创建且从未承载 trial
- [ ] 池内实例抽查：创建时间、快照 ID、trial 历史三者一致
- [ ] 预热池实例同样要过 `root_readonly`/非 root/私有 tmpfs 检查（预检 I1–I9 不豁免）

## 6. 签字

| 设备/宿主 | pool | 租约 ID | 结论 | 证据 URI | 检查人 | 时间 |
| --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |
