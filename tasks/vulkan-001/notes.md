# vulkan-001 答案与验收说明

> 状态：v1 · 维护：research · 最后更新：2026-11-06
> 上游：[00-conventions](../../../docs/00-conventions.md) · 相关：[vulkan-gpu playbook](../../playbooks/vulkan-gpu.md)、[05-platform-notes](../../../research/05-platform-notes.md)

## 1. 结构

    vulkan-001/
    ├── task.yaml / instruction.md / persona.md / rubric.yaml / defects.yaml
    ├── canary.md / notes.md
    ├── starter/
    │   ├── README.md                 # 契约 + 构建/运行命令
    │   ├── CMakeLists.txt            # glslc 编译 4 个着色器
    │   ├── src/main.cpp              # Vulkan 骨架（4 处 TODO：验证层/深度/阴影/截图）
    │   ├── src/shadow_math.h         # 阴影数学契约（哈希锁定）
    │   ├── src/shadow_math.cpp       # 空壳（要补）
    │   └── shaders/{scene.vert,scene.frag,shadow.vert,shadow.frag}
    ├── answers/{reference-a.patch, reference-b.patch, empty-agent.md}
    └── hidden-tests/
        ├── run.sh                    # 默认 / --gpu / --screenshot / --selftest
        ├── static_checks.py          # 结构/验证层/阴影资源/着色器（含 --locks-only）
        ├── image_metrics.py          # PPM/PNG 解码 + MAD/PSNR/匹配率（纯标准库）
        ├── locks.json                # shadow_math.h sha256
        ├── refs/README.md            # 参考图生成说明（当前无参考图）
        └── tests/{test_shadow_math.cpp,test_image_metrics.py}

## 2. 环境与依赖

- GPU 节点：Linux 容器 + Vulkan SDK 1.3.290（含 glslc）+ 驱动；`gpu: 1»。
- 无 GPU 宿主：只能跑静态检查 + CPU 数学 + 图像工具自测（本机即如此，clang 16 实测）。
- 截图参考图需在 GPU 宿主机生成一次后放进 `hidden-tests/refs/»：
  **TODO(run-on-host): 在 GPU 节点生成 refs/scene_reference.ppm 并复核阈值为 MAD≤12 / ratio≥0.92**。

## 3. 验证命令

    # 隐藏验收（默认 starter，预期失败；无参考图时截图段打印 SKIP）
    bash hidden-tests/run.sh

    # 自检：参考解 A/B 通过、starter 失败、契约篡改被拦截
    bash hidden-tests/run.sh --selftest

    # 只校验契约文件哈希（HG3）
    python3 hidden-tests/static_checks.py --locks-only

    # GPU 节点：构建 + 运行 + 截图 + 比对
    bash hidden-tests/run.sh --gpu

## 4. 实测证据（2026-11-06，本机 python3 3.15.2 / Apple clang 16.0.0，无 Vulkan 工具链）

    $ bash hidden-tests/run.sh --selftest
    vulkan-001 selftest
      [OK]   reference-a：静态+CPU 数学通过（预期通过）
      [OK]   reference-b：静态+CPU 数学通过（预期通过）
      [OK]   starter：静态检查失败（预期失败）
      [OK]   starter：CPU 数学测试失败（预期失败）
      [OK]   契约文件被修改：哈希校验拦截（预期拦截）
    selftest: 5 通过 / 0 失败
    SELFTEST PASS

    $ python3 hidden-tests/tests/test_image_metrics.py
      ... 7 通过 / 0 失败

CPU 数学测试 22 项断言：单位矩阵/正交投影/视图矩阵/光源矩阵/PCF 共 5 组；参考解 A/B 两种实现均通过。
GPU 段（构建/运行/验证层/截图）尚未在本机执行——见 §2 待办。

## 5. 答案说明

| 参考解 | 阴影结构 | CPU 侧实现 |
| --- | --- | --- |
| A | 单命令缓冲内两段渲染（阴影子通道 + 主通道），PCF 用 texture() 3x3 循环 | 列主序直接构造矩阵 |
| B | 阴影子通道拆成独立函数，PCF 用 textureOffset 九连击 | 行向量累加 + 预计算 texel 步长 |

应用补丁：`cd starter && patch -p1 < ../answers/reference-a.patch»。

## 6. 已知容差

- 截图判定：mean_abs_diff ≤ 12、逐通道匹配率（diff≤8）≥ 0.92；不同 GPU 的 AA/精度差异由阈值吸收。
- 未提供参考图时不判截图（SKIP），但 HG4 在验收环境必须有参考图，否则该题不得上线。
- CPU 数学为精确断言（浮点容差 1e-4），PCF 比例为精确 1/9 等分数。

## 7. 版本与平台

- 固定 Vulkan SDK 1.3.290 与驱动版本；镜像 digest 待固定（见 task.yaml resources.image_digest）。
- 只在 GPU 节点采性能/截图；lavapipe 仅功能验证（docs/05 §4）。
- GPU 节点独占：每 trial 一个容器，限制显存与算力，防止同节点互相干扰计时（docs/04 §5.1）。

## 8. 隔离要点

- Agent 环境 separate、`no-network»；SPIR-V 与截图作为 artifacts 进验证沙箱；
- 验证层日志与截图必须由 verifier 侧采集，不能只信 Agent 自报；
- 金丝雀注入见 canary.md（不得写进哈希锁定文件）；
- GPU 节点归还：容器销毁 + 显存/进程检查，见 [vulkan-gpu playbook](../../playbooks/vulkan-gpu.md) §5。
