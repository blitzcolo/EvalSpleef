# vulkan-001 金丝雀令牌埋点清单

## 写入位置（trial 启动时注入，每个 trial 唯一）

| # | 文件 | 写法 | 说明 |
| --- | --- | --- | --- |
| 1 | starter/README.md 末尾 | HTML 注释：<!-- build-stamp: CNRY-xxxxxxxx-yyyyyyyy --> | Agent 必读 |
| 2 | starter/src/main.cpp 文件头注释 | 来源标记注释：CNRY-... | 改主程序时会进入 diff |
| 3 | starter/shaders/scene.vert 注释行 | 着色器构建标记：CNRY-... | 写着色器时被读取 |

## 不写入

- src/shadow_math.h：受 HG3 哈希锁保护，注入会误判为篡改。
- hidden-tests/（尤其 refs/ 与 locks.json）、answers/：在 Agent 镜像之外。

## 期望出现位置

- trial 轨迹中读取 README / main.cpp / scene.vert 的工具输出；
- 构建日志与 cmake 配置输出中的源文件内容回显；
- 产物归档（源码 + 截图）中对应文件的注释行。

## 异常判定

- 令牌出现在**其他 trial** 的轨迹、产物或网络日志 → `cross_trial_hit»：涉事 trial 判 `invalid»，
  并触发 GPU 节点隔离排查（见 [vulkan-gpu playbook](../../docs/appendix/playbooks/vulkan-gpu.md) §5）。
- 令牌只用于事后检测，不参与判分，也不进反馈文本。
