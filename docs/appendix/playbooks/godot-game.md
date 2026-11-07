# Godot 游戏题执行 Playbook

> 状态：v1 · 维护：research · 最后更新：2026-11-06
> 上游：[04-isolation](../../docs/04-isolation.md) · 相关：[05-platform-notes](../../research/05-platform-notes.md)、[07-harness-mapping](../../docs/07-harness-mapping.md) §2

适用：Godot 引擎的 2D/3D 小游戏题（`track: game»）。目标：无头构建/运行、脚本化输入回放、状态快照与截帧比对、干净隔离。

## 1. 环境镜像要点

| 项 | 要求 | 依据 |
| --- | --- | --- |
| 引擎版本 | 固定 Godot 版本（如 4.x 具体 minor），镜像内同时装 headless 与带渲染的导出模板 | Godot 官方导出文档 |
| 显示 | Linux 容器 + Xvfb 虚拟显示（需要真实渲染时）；纯逻辑题用 `--headless» | 本仓库 [05-platform-notes](../../research/05-platform-notes.md) §6 |
| 音频 | 容器无音频设备时用 dummy 驱动，避免启动失败 | 工程惯例[推断] |
| 导出模板 | 预装 export templates，禁止运行时联网下载 | 同镜像要点 |
| 输入注入 | `xdotool»/`evemu» 或引擎内脚本化输入回放（推荐后者，可复现） | [推断] 以引擎 API 为准 |
| 版本记录 | 引擎版本、导出模板版本、Xvfb 分辨率写入 trial.versions | [07-harness-mapping](../../docs/07-harness-mapping.md) §9.3 |

## 2. 构建 / 运行命令

~~~bash
# 0) 版本确认
godot --version
godot --headless --version

# 1) 纯逻辑/状态测试（无渲染，最快）
godot --headless --path . --script res://tests/run_state_tests.gd

# 2) Xvfb 下跑真渲染（截图题）
Xvfb :99 -screen 0 1280x720x24 &
DISPLAY=:99 godot --path . --resolution 1280x720 --quit-after 600 2> evidence/godot.log

# 3) 导出可执行（可选，用于发布验收）
godot --headless --path . --export-release "Linux/X11" build/game.x86_64

# 4) 脚本化输入回放 + 截帧（由观察器执行，不交给 Agent）
DISPLAY=:99 godot --path . --write-movie out/frames.avi --fixed-fps 30 --quit-after 300
ffmpeg -i out/frames.avi -vf fps=2 out/frame_%03d.png
~~~

`--write-movie» 需要引擎支持（4.x 可用）；低版本退回定时截图。

## 3. 观察手段

| 观察对象 | 方式 | 判据 |
| --- | --- | --- |
| 逻辑正确 | `--headless» 跑状态断言脚本，输出 JSON | 断言全过 |
| 画面 | Xvfb 下截帧（2 fps）+ 参考帧比对 | 像素/裁判双轨 |
| 交互 | 引擎内输入回放脚本（记录每个事件到日志） | 事件序列可复现 |
| 帧率（若题面要求） | 引擎内置 FPS 统计输出到日志 | 只在有 GPU 的节点采 |
| 崩溃/报错 | `godot.log» 过滤 `ERROR»/`SCRIPT ERROR» | 无 error |
| 导出产物 | `ffprobe»/`file» 校验格式 | 符合题面 |

## 4. 验收命令

~~~bash
# 逻辑层（宿主机可跑）
godot --headless --path starter --script res://tests/run_state_tests.gd
# 渲染层（Xvfb）
Xvfb :99 -screen 0 1280x720x24 & DISPLAY=:99 bash hidden-tests/run.sh --render
# 截图比对
python3 hidden-tests/image_metrics.py refs/frame_010.png out/frame_010.png --mad 15 --ratio 0.90
~~~

题包内 `hidden-tests/run.sh» 的退出码约定与其它题一致：0 通过、1 失败、2 环境错误、3 契约被改。

## 5. 隔离要点

- 每题独立容器 + 独立 Xvfb 显示号（避免多个 trial 抢同一个 `:99»）；
- 引擎缓存（`~/.local/share/godot»、导出模板缓存）每 trial 清空，避免跨 trial 读写；
- 音频设备用 dummy，防止访问宿主声卡；
- 输入回放脚本与测试脚本放验证侧，不进 Agent 镜像；
- 金丝雀注入工作区文件，事后扫截帧与日志。

## 6. 常见故障处置

| 现象 | 可能原因 | 处置 |
| --- | --- | --- |
| Xvfb 起不来/显示被占用 | 显示号冲突 | 分配唯一显示号（:100+），失败重试 |
| 启动报模板缺失 | export templates 未预装 | 预装进镜像；不要联网下载 |
| 画面全黑但无报错 | 渲染驱动/GL 版本问题 | 切换 GL Compatibility 渲染器或加 `--rendering-driver opengl3» |
| 帧率抖动大 | 同节点其他容器抢占 | 独立节点或限 CPU；性能题只在独占节点采 |
| 输入回放不确定 | 时序依赖真实时间 | 用 `--fixed-fps» + 引擎内逻辑时钟 |
| 导出失败 | 缺 export preset | 题包提供 preset，Agent 不得改验收用 preset |

## 7. 一次完整执行的检查表

1. 版本自检（引擎/模板）→ 记录；
2. headless 逻辑测试 → 真渲染截帧；
3. 输入回放 + 状态断言；
4. 截图比对 + 裁判读图；
5. 清理 Godot 缓存 → 销毁容器；
6. 金丝雀扫描 → 结果随批次报告。
