# harmony-001 答案与验收说明

> 状态：v1 · 维护：research · 最后更新：2026-11-06
> 上游：[00-conventions](../../../docs/00-conventions.md) · 相关：[harmony-device-pool playbook](../../playbooks/harmony-device-pool.md)、[05-platform-notes](../../../research/05-platform-notes.md)

## 1. 结构

    harmony-001/
    ├── task.yaml / instruction.md / persona.md / rubric.yaml / defects.yaml
    ├── canary.md / notes.md
    ├── starter/                         # Agent 可见工作区
    │   ├── README.md                    # 契约说明 + 构建/装机命令
    │   ├── build-profile.json5          # 签名配置占位（证书由平台注入）
    │   ├── AppScope/ entry/src/main/... # 标准 Stage 模型工程
    │   └── entry/src/main/cpp/
    │       ├── image_ops.h              # 内核接口契约（哈希锁定，不得改）
    │       ├── image_ops.cpp            # 空壳实现（要补）
    │       ├── napi_init.cpp            # NAPI 绑定（只注册了占位函数）
    │       └── types/libimageops/index.d.ts  # ArkTS 类型契约（哈希锁定）
    ├── answers/{reference-a.patch, reference-b.patch, empty-agent.md}
    └── hidden-tests/
        ├── run.sh                       # 三模式：默认 / --device / --selftest
        ├── static_checks.py             # 静态检查（含 --locks-only）
        ├── locks.json                   # 契约文件 sha256
        └── tests/test_image_ops.cpp     # C++ 数值测试（25 项断言）

## 2. 环境与依赖

- 构建：Linux 容器 + HarmonyOS commandline-tools（hvigorw / ohpm / hdc），SDK 6.0.2(22)；
  签名材料由平台挂到 /opt/harmony-signing，证书/Profile 不进仓库。
- 运行：鸿蒙真机设备池（独占租用），控制器执行 `hdc install» / `aa start» / `hilog»。
- 本地可复现部分：C++ 内核数值测试（clang++/g++，本机 clang 16 实测）+ 静态检查。
  构建、签名、装机、截图属于设备侧步骤：**TODO(run-on-host): 在设备侧执行器跑 --device 并归档 hilog**。

## 3. 验证命令

    # 隐藏验收（默认 starter，预期失败）
    bash hidden-tests/run.sh

    # 自检：参考解 A/B 通过、starter 失败、契约篡改被拦截
    bash hidden-tests/run.sh --selftest

    # 只校验契约文件哈希（HG3）
    python3 hidden-tests/static_checks.py --locks-only

    # 设备侧（需要工具链 + 真机）
    bash hidden-tests/run.sh --device

## 4. 实测证据（2026-11-06，本机 python3 3.15.2 / Apple clang 16.0.0）

    $ bash hidden-tests/run.sh --selftest
    harmony-001 selftest
      [OK]   reference-a：静态+数值通过（预期通过）
      [OK]   reference-b：静态+数值通过（预期通过）
      [OK]   starter：静态检查失败（预期失败）
      [OK]   starter：数值测试失败（预期失败）
      [OK]   契约文件被修改：哈希校验拦截（预期拦截）
    selftest: 5 通过 / 0 失败
    SELFTEST PASS

数值测试覆盖：灰度（3 项）、提亮（3 项）、对比度（5 项）、模糊（4 项）、旋转（7 项），共 25 项，全部通过。
参考解 A/B 两种实现（直接逐像素 vs LUT+可分离模糊）——证明验收不认死一种写法。

## 5. 答案说明

| 参考解 | 思路 | 与另一解的差异 |
| --- | --- | --- |
| A | 逐像素整数定点；NAPI 逐个绑定；UI 单入口 applyOp(kind) | 直接、好读 |
| B | 256 项 LUT（亮度/对比度）、可分离两趟模糊、按目标坐标反查旋转；NAPI 用函数指针表 | 查表与两趟分解，性能路径不同 |

应用补丁：`cd starter && patch -p1 < ../answers/reference-a.patch»。

## 6. 已知容差

- 灰度/对比度/模糊按 ±1 色阶容差判定（四舍五入实现差异），提亮为精确值，旋转为精确值。
- 大图性能不设硬阈值；R10 只要求"处理期间有可见反馈"。

## 7. 版本与签名

- 固定 SDK 6.0.2(22)、commandline-tools 版本随镜像 digest 锁定；镜像 digest 待宿主机固定
  （见 task.yaml 的 resources.image_digest）。
- 签名材料：`/opt/harmony-signing/{default.cer, default.p12, default.p7b}»，密码由执行器环境变量注入；
  Agent 不得申请开发者账号、不得修改签名配置中的路径（只允许按平台说明读取）。

## 8. 隔离要点

- 设备独占租用，归还按 [device-pool-handover](../../checklists/device-pool-handover.md) 执行；
- Agent 不直接持有 `hdc» 全权（只能通过受限代理执行安装/启动/截图）；
- 金丝雀注入位置见 canary.md，注意**不得**写进两个哈希锁定文件；
- 构建容器断网（docs 白名单由平台代理提供），依赖预装进镜像。
