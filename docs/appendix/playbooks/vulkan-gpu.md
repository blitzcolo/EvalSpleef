# Vulkan / GPU 节点执行 Playbook

> 状态：v1 · 维护：research · 最后更新：2026-11-06
> 上游：[04-isolation](../../docs/04-isolation.md) · 相关：[05-platform-notes](../../research/05-platform-notes.md) §4-§5、[vulkan-001](../examples/vulkan-001/task.yaml)、[vulkan-001 notes](../examples/vulkan-001/notes.md)

适用：`platform: gpu-node» 的图形题（Vulkan 管线/着色器/性能）。目标：构建、跑起来、验证层干净、拿到可复现截图，并把 GPU 节点干净归还。

## 1. 环境镜像要点

| 项 | 要求 | 依据 |
| --- | --- | --- |
| 基础镜像 | Linux + Vulkan SDK `1.3.290»（含 `glslc»/`vulkaninfo»）+ 固定版本驱动 | [Vulkan Validation Layers](https://github.com/KhronosGroup/Vulkan-ValidationLayers) |
| 构建依赖 | `cmake ≥ 3.20»、`glslc»、C++17 编译器，全部预装（构建期断网） | [vulkan-001 README](../examples/vulkan-001/starter/README.md) |
| 软件渲染后备 | Mesa lavapipe（`VK_ICD_FILENAMES» 指向 lavapipe icd），**只做功能验证，不采性能** | [05-platform-notes](../../research/05-platform-notes.md) §4 |
| 验证层 | 强制启用 `VK_LAYER_KHRONOS_validation» + debug messenger；error 即失败 | 同 §4 |
| GPU 配额 | Harbor 的 `gpus»/`gpu_types» 透传给支持 GPU 的 provider（daytona/modal/gke/beam/opensandbox/prime/docker），显存隔离由 provider 负责 | [Harbor resources 文档](https://docs.harborframework.com/tasks/resources.md)、[Daytona limits](https://www.daytona.io/docs/en/limits/) |
| 软件栈锁定 | 驱动版本、SDK 版本、lavapipe 版本都写进镜像 tag 并记录到 trial.versions | [07-harness-mapping](../../docs/07-harness-mapping.md) §9 |

## 2. 构建 / 运行命令

~~~bash
# 0) 环境自检
vulkaninfo --summary | head -20        # 期望能看到 GPU 与驱动版本
glslc --version

# 1) 构建
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j

# 2) 运行并出图（120 帧足够稳定一帧阴影）
./build/vulkan_shadow --frames 120 --screenshot out/scene.ppm

# 3) 验证层日志单独落盘（stderr）
./build/vulkan_shadow --frames 120 2> evidence/validation.log

# 4) 软件渲染兜底（无 GPU 时）
VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.json ./build/vulkan_shadow --frames 5 --screenshot out/scene_lavapipe.ppm
~~~

## 3. 观察手段

| 观察对象 | 方式 | 判据 |
| --- | --- | --- |
| 验证层 | `grep -c "VUID" evidence/validation.log» | error 数为 0（warning 记账不判失败） |
| 画面 | `out/scene.ppm» 截图 + 参考图比对 | MAD ≤ 12、逐通道匹配率 ≥ 0.92 |
| 阴影方向/软硬 | 裁判读图（rubric R8/R9） | 影子在背光侧、边缘有过渡 |
| 帧时间（性能题） | `xctrace»/`vulkaninfo» 不适用；Linux 用 API timestamp query 或「帧时间 CSV」 | 只在 GPU 节点采，记录 5 次中位数 |
| GPU 占用 | `nvidia-smi»/`rocm-smi» 或容器 cgroup | 归还前无残留进程 |
| SPIR-V | 归档 `build/shaders/*.spv» 与源码哈希 | 防止「源码与产物不一致」 |

## 4. 验收命令

~~~bash
cd appendix/examples/vulkan-001
bash hidden-tests/run.sh                    # 静态 + CPU 阴影数学 + 截图工具（宿主机可跑）
bash hidden-tests/run.sh --gpu              # cmake 构建 + 运行 + 截图 + 比对（GPU 节点）
python3 hidden-tests/static_checks.py --locks-only    # 契约文件未被修改
python3 hidden-tests/image_metrics.py hidden-tests/refs/scene_reference.ppm out/scene.ppm --mad 12 --ratio 0.92
~~~

退出码：0 通过、1 失败、2 环境/用法错误、3 契约被改。GPU 段日志、SPIR-V、截图作为 artifacts 进验证沙箱。

## 5. 隔离要点

- **节点独占**：一个 GPU 容器只服务一个 trial；限制显存/算力（MIG 或 provider 配额），防止同节点计时互相干扰。
- **不采性能的数据**：lavapipe / 软件渲染下的帧时间不作为任何成绩依据。
- **产物只读**：agent 工作区 → artifacts（截图、SPIR-V、日志）→ 验证沙箱；验证侧不读 agent 其他文件。
- **金丝雀**：注入 README / main.cpp / 着色器注释（不得写进哈希锁定文件），事后跨 trial 扫描。
- **归还**：销毁容器 → 检查残留进程/显存 → 节点健康检查；异常记入 [trial-hygiene](../checklists/trial-hygiene.md)。

## 6. 常见故障处置

| 现象 | 可能原因 | 处置 |
| --- | --- | --- |
| `vulkaninfo» 失败 | 无驱动/无 GPU 透传 | 检查 provider GPU 声明与驱动；退回 lavapipe 只跑功能 |
| 验证层大量 VUID | 资源生命周期/同步错误 | 判为模型侧缺陷（R2），不自动重跑；把日志作为 F2 素材 |
| 截图全黑 | 未 present/未等帧、交换链格式错 | 检查截图路径是否在 present 之后；仍黑则判 R7 |
| 相似度略低（0.88~0.92） | 不同 GPU 的 AA 差异 | 先看人工复核是否符合描述；必要时按允许范围调阈值（须记录） |
| lavapipe 上跑不出阴影 | 软件实现精度/扩展差异 | 只作功能参考；阴影判定以 GPU 节点为准 |
| cmake 找不到 Vulkan | 镜像缺 SDK 或环境变量 | 预装 SDK 并固定 `VULKAN_SDK»；不要联网下载 |
| GPU 节点归还后显存残留 | 进程未退出 | 强制 kill + 检查 `nvidia-smi»；连续两次摘除节点 |
| 同一截图两次不一致 | 卡未固定/随机种子 | 固定渲染目标分辨率与帧数；记录驱动版本 |

## 7. 一次完整执行的检查表

1. 节点自检（`vulkaninfo» + `glslc» 版本）→ 记录到 trial.versions；
2. 构建 → 运行 120 帧 → 保存验证层日志与截图；
3. 跑 `hidden-tests/run.sh --gpu»；
4. 截图比对 + 裁判读图；
5. 销毁容器 → 残留检查 → 节点回池；
6. 金丝雀扫描 → 结果随批次报告。
