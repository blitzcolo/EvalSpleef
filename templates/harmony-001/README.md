# harmony-001 模板构建说明

## 1. 为什么该环境无法在本地构建

根据 `tasks/harmony-001/task.yaml`、`tasks/harmony-001/notes.md` 以及 `docs/appendix/playbooks/harmony-device-pool.md`，该评测环境无法在当前通用构建环境/本地开发机中直接构建：

1. **专有闭源工具链与 SDK**：
   - 构建需要华为 HarmonyOS commandline-tools（`hvigorw`、`ohpm`、`hdc`、`node`）以及固定版本 SDK `6.0.2 (API 22)`。
   - 华为开发者工具链与 SDK 需要华为开发者账号认证、签署许可协议后下载，无法通过公共包管理器无交互拉取。
2. **签名材料与凭据**：
   - 编译产出可安装的 HAP 包需要专用证书与签名档案（`/opt/harmony-signing/{default.cer, default.p12, default.p7b}`），其密码由执行器环境变量在受信构建机动态注入，不随公共镜像提交。
3. **硬件依赖（鸿蒙设备池）**：
   - 题目平台声明为 `platform: harmony-device-pool`（L4 隔离），实际运行与验收需要物理 ARM64 鸿蒙真机或内网专有模拟器集群，并通过 `hdc` 经受限代理操作。本地工作站（macOS/Linux）缺少物理设备连接与硬件测试环境。
4. **网络与依赖烘焙**：
   - 题目在沙箱断网条件下运行，所有 `ohpm` 依赖及预编译 NAPI 二进制必须在具备华为私有仓库访问权限的受信内网构建机预先烘焙进镜像。

## 2. 生产环境构建规范（参考）

若在具备授权的受信构建机上构建，镜像规范如下：

```dockerfile
# 基础镜像：具备 glibc 与 Node.js 的 Linux 环境
FROM ubuntu:24.04

# 安装 commandline-tools 与 HarmonyOS SDK 6.0.2(22)
# 配置 OHPM_HOME, HOS_SDK_HOME, PATH

# 创建非 root 评测用户 agent (uid 10001)
RUN useradd -u 10001 -m -s /bin/bash agent && \
    mkdir -p /workspace && chown -R agent:agent /workspace

# 挂载/注入签名工具与依赖
# 移除 sudo 及 setuid 提权位
```

详情参阅：
- `TECH-DESIGN.md` §7.4.9 (`device-pool`)
- `PRD.md` FR-SBX-09
- `docs/appendix/playbooks/harmony-device-pool.md`
