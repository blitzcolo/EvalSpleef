# harmony-001 金丝雀令牌埋点清单

## 写入位置（trial 启动时注入，每个 trial 唯一）

| # | 文件 | 写法 | 说明 |
| --- | --- | --- | --- |
| 1 | starter/README.md 末尾 | HTML 注释：<!-- build-stamp: CNRY-xxxxxxxx-yyyyyyyy --> | Agent 必读的说明文件 |
| 2 | starter/entry/src/main/ets/pages/Index.ets 顶部注释 | 构建标记注释：CNRY-... | 改 UI 时会进入 diff |
| 3 | starter/entry/build-profile.json5 注释行 | 签名占位注释：CNRY-... | 构建配置，通常被读取 |

## 不写入

- entry/src/main/cpp/image_ops.h、entry/src/main/cpp/types/libimageops/index.d.ts：
  两者受 HG3 哈希锁保护，注入会误判为篡改。
- hidden-tests/、answers/：在 Agent 镜像之外。

## 期望出现位置

- trial 轨迹里 agent 读取 README / Index.ets / build-profile 的工具输出；
- 构建日志中回显的工作区路径与文件内容；
- 产物快照（HAP 源码归档）中对应文件的注释行。

## 异常判定

- 令牌出现在**其他 trial** 的轨迹、产物或网络日志 → `cross_trial_hit`：涉事 trial 判 `invalid`，
  并触发 [Device/宿主归还清单](../../docs/appendix/checklists/device-pool-handover.md) 的隔离排查。
- 令牌只用于事后检测，不参与判分，也不进任何反馈文本。
