# 鸿蒙设备池执行 Playbook

> 状态：v1 · 维护：research · 最后更新：2026-11-06
> 上游：[04-isolation](../../docs/04-isolation.md) · 相关：[device-pool-handover](../checklists/device-pool-handover.md)、[05-platform-notes](../../research/05-platform-notes.md) §3、[harmony-001](../examples/harmony-001/task.yaml)

适用：`platform: harmony-device-pool» 的题（构建在 Linux 容器，运行在鸿蒙真机/模拟器池）。
目标：让执行者照着做就能把一个 HAP 从源码跑到设备上、采到证据、干净归还。

## 1. 环境镜像要点

| 项 | 要求 | 依据 |
| --- | --- | --- |
| 构建容器 | Linux + HarmonyOS commandline-tools（hvigorw / ohpm / hdc / node / sdk），SDK 固定 `6.0.2(22)» | [华为命令行构建文档](https://developer.huawei.com/consumer/cn/doc/doccenter-deveco-studio/ide-hmos-command-line-building-app)（JS 渲染页，抓取时仅得壳） |
| 签名材料 | `/opt/harmony-signing/{default.cer,default.p12,default.p7b}» 只读挂载；密码用执行器环境变量注入 | [harmony-001 notes](../examples/harmony-001/notes.md) §7 |
| 依赖预装 | `ohpm install --all» 所需包必须烘进镜像；构建期默认断网 | 同 notes §8 |
| 设备镜像 | 真机保持基线版本；模拟器受地域限制（中国大陆以外不提供手机/平板/手表下载，手表除外） | [SDL HarmonyOS 说明](https://wiki.libsdl.org/SDL3/README-harmonyos) |
| 工具权限 | Agent 只经受限代理拿到 `hdc install/start/screenshot» 三个动作，不给全权 shell | [04-isolation](../../docs/04-isolation.md) §6 |
| 账号 | Agent 不得注册华为开发者账号；证书更新由人做 | 同上 |

## 2. 构建 / 运行命令

~~~bash
# 0) 依赖
ohpm install --all

# 1) 构建 debug HAP
hvigorw clean --no-daemon
hvigorw assembleHap --mode module -p product=default -p buildMode=debug --no-daemon
ls entry/build/default/outputs/default/*-signed.hap

# 2) 设备确认与安装
hdc list targets
hdc install -r entry/build/default/outputs/default/entry-default-signed.hap

# 3) 启动 + 抓日志（10 秒窗口）
hdc shell aa start -a EntryAbility -b <bundleName>
sleep 10
hdc shell hilog -T <tag> -t app > /tmp/hilog.txt

# 4) 截图（观察器侧执行，不交给 Agent）
hdc shell snapshot_display -f /data/local/tmp/shot.jpeg
hdc file recv /data/local/tmp/shot.jpeg ./evidence/shot.jpeg
~~~

`snapshot_display» 属于设备侧工具，参数随系统版本略有差异；执行前用 `hdc shell snapshot_display -h» 确认。

## 3. 观察手段

| 观察对象 | 命令/方式 | 判据 |
| --- | --- | --- |
| 是否装上 | `hdc shell bm dump -n <bundleName>»（子命令以设备 `bm -h» 为准） | 返回包信息 |
| 是否存活 | `hdc shell ps -ef | grep <bundleName>» | 进程存在 |
| 崩溃 | `hilog -T <tag> -t app» 过滤 `CppCrash»/`AppRecover»/`SIGSEGV» | 无命中 |
| 画面 | 截图 + 与参考帧比对（LLM/像素） | 主观条目由裁判读图 |
| 性能 | `hdc shell hidumper -s <service>» 或应用内埋点 | 只在真机采，模拟器不采性能 |
| 资源 | `hdc shell aa dump -a»、`bm dump» | 归还时与基线比对 |

## 4. 验收命令

~~~bash
cd appendix/examples/harmony-001
bash hidden-tests/run.sh            # 静态 + C++ 数值（宿主机可跑）
bash hidden-tests/run.sh --device   # 构建/签名/安装/启动（设备侧执行器）
python3 hidden-tests/static_checks.py --locks-only   # 契约文件未被修改
~~~

退出码：0 通过、1 失败、2 环境/用法错误、3 契约被改（作弊）。日志与截图作为 artifacts 交给验证沙箱。

## 5. 隔离要点

- **设备独占**：一个设备同时只服务一个 trial；租约状态 `free → in_use → handover_check»。
- **归还清理**：`aa force-stop» → 卸载非基线包 → 清 `/data/local/tmp/*» → 包列表与基线 diff → 必要时重启/恢复出厂。
  完整步骤见 [device-pool-handover](../checklists/device-pool-handover.md)。
- **Agent 无设备 shell**：只走受限代理；任何直接 `hdc» 调用都应被拒绝并记录。
- **金丝雀**：注入工作区文件（不得写进哈希锁定文件），事后扫轨迹与其他 trial 的截图/日志。
- **网络**：构建容器仅文件白名单（`developer.huawei.com» 等）；运行期设备默认无网络。

## 6. 常见故障处置

| 现象 | 可能原因 | 处置 |
| --- | --- | --- |
| `ohpm install» 卡住/失败 | 镜像缺依赖且断网 | 预装依赖进镜像；或临时开 package mirror 白名单（记录） |
| 构建报签名错误 | 证书未挂载/密码未注入 | 检查 `/opt/harmony-signing» 与执行器环境变量；不要改 build-profile 路径 |
| `hdc list targets» 为空 | USB 授权过期/无线调试断连 | 重新插拔或 `hdc tconn <ip>:<port>»；仍失败则摘除设备 |
| 安装报 `9568xxx» 错误 | 签名不匹配/系统版本不符 | 核对 SDK 与设备基线版本；不要绕过签名 |
| 启动即退 | `CppCrash» | 判为模型侧错误（成绩有效），记 R2 缺陷；不要自动重跑 |
| 截图失败 | 设备侧 `snapshot_display» 参数不符 | 先 `-h» 确认；退回 `hdc shell uitest screenCap» 方案 |
| 归还 diff 非空 | 上一轮残留 | 卸载残留包并记一次隔离事件；连续两次则摘除设备 |
| 设备在池中但一直忙 | 租约泄漏 | 按心跳超时收回；连续发生则重启设备管理进程 |

## 7. 一次完整执行的检查表

1. 取租约 → 记录设备 ID 与基线哈希；
2. 构建 → 安装 → 启动 → 抓 10 秒日志；
3. 观察器截图 + 记录现象；
4. 跑 `hidden-tests/run.sh --device»；
5. 归还清理 → 写 `handover.json»；
6. 金丝雀扫描 → 结果随批次报告。
