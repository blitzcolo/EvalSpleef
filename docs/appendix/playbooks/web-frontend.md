# Playbook · Web 前端 / 3D 可视化

> 状态：v1 · 维护：tasks-b · 最后更新：2026-11-06
> 上游：[00-conventions](../../00-conventions.md) · 相关：[02-eval-flow](../../02-eval-flow.md)、[04-isolation](../../04-isolation.md)、[05-task-library](../../05-task-library.md)

面向赛道：web（前端设计、交互、3D 建模、2D 动画、科学可视化）。示例题包：[web-001](../../../tasks/web-001/task.yaml)（tasks-a 维护）。

## 1. 环境镜像要点

| 项 | 建议 | 说明 |
| --- | --- | --- |
| 基础镜像 | node:22-bookworm-slim + Chromium 固定版本 | 按 digest 固定；Node 与浏览器版本随批次记录 |
| 包管理 | pnpm / npm 依赖**预装进镜像**，配只读 npm 缓存 | 每 trial 新建 node_modules 层，不共享写缓存 |
| 字体 | fonts-noto-cjk、fonts-dejavu | 缺字体导致截图文字变方块，直接误判视觉分 |
| 无头浏览器 | Playwright 固定 minor（自带 Chromium） | 不用宿主 Chrome，避免版本漂移（[Playwright 浏览器](https://playwright.dev/docs/browsers)） |
| 网络 | 默认断网；只读包镜像 + 文档白名单 | 文档白名单只放官方文档站 |
| 资源 | 2 vCPU / 4 GiB / 10 GiB | WebGL 题给 4 vCPU；软件渲染更吃 CPU |

## 2. 构建与运行命令

     # 安装（镜像内已缓存依赖时加 --offline / --frozen-lockfile）
     pnpm install --frozen-lockfile
     # 构建
     pnpm build
     # 起本地静态服务（每个 trial 随机端口，禁止共享）
     python3 -m http.server 0 --bind 127.0.0.1 --directory dist &
     # 记录实际端口，供观察器与截图使用

服务必须以 **127.0.0.1 + 随机端口** 起在 trial 私有网络里；验证脚本从环境变量读取 base URL，不写死 3000/5173。

## 3. 观察手段清单（供 Observer 直接引用）

| 观察项 | 手段 | 命令 / API | 证据形式 |
| --- | --- | --- | --- |
| 首屏截图 | Playwright 固定视口 1440x900、deviceScaleFactor=1 | page.screenshot({fullPage:true})（[截图文档](https://playwright.dev/docs/screenshots)） | PNG + 尺寸 + sha256 |
| 交互截图 | 脚本化点击/拖拽后截图 | page.mouse.move/down/up；page.click | 3 张连续截图 |
| 动画逐帧 | 固定页面时钟逐帧推进 | page.clock.install + fastForward（[Clock 文档](https://playwright.dev/docs/clock)） | t=0.5s/1.5s/3s 截图 |
| 无障碍树 | Playwright accessibility snapshot | page.accessibility.snapshot()（[无障碍测试](https://playwright.dev/docs/accessibility-testing)） | JSON 快照 |
| 控制台错误 | page.on('console') / page.on('pageerror') | 收集 error 级日志 | JSONL |
| 请求失败 | page.on('requestfailed') | 记录 URL/失败原因 | JSONL |
| 布局尺寸 | evaluate 读取 getBoundingClientRect | page.evaluate | JSON |
| 视觉回归 | 截图与基线逐像素差异率 | Python 标准库解析 PNG（无 Pillow 时存 PPM） | diff 百分比 |

截图前统一：禁用动画（prefers-reduced-motion）、隐藏光标、固定 locale/时区（TZ=UTC）、固定随机种子。

## 4. 验收命令

     # 隐藏验收：交互用例 + 截图 + a11y + 视觉断言
     bash hidden-tests/run.sh
     # 自检：参考解 A/B 通过、空 Agent 失败、截图基线可复现
     bash hidden-tests/run.sh --selftest
     # 单次重判（不重跑 Agent）
     python3 scripts/regrade.py --trial <trial_id> --reason verification_flake

硬门槛建议：构建成功、页面 200、核心交互用例全过、无 console error、截图 sha256 可复现（同 trial 内两次一致）。

## 5. 隔离要点

1. 每 trial 独立容器 + 独立 node_modules 层；浏览器 user-data-dir 放 /tmp 私有 tmpfs。
2. dev server 只用 127.0.0.1 + 随机端口；不共享 host 网络，不暴露端口到宿主。
3. 截图与基线只在验证侧产出；Agent 镜像内不得出现隐藏截图基线。
4. WebGL 正确性在 CPU 渲染（SwiftShader）下验，性能类指标单独放 GPU 节点（见 [04-isolation](../../04-isolation.md)）。
5. 前后两 trial 的浏览器缓存目录不得复用（用前清空或全新 $HOME）。

## 6. 常见故障与处置

| 现象 | 根因 | 处置 |
| --- | --- | --- |
| 截图文字全是方块 | 镜像缺 CJK 字体 | 镜像预装 Noto CJK；重跑该 trial（environment，retry） |
| 截图每次差几个像素 | 动画/字体渲染未固定 | 禁用动画、固定视口与 dpr；仍抖动则放宽阈值并 human 复核 |
| 页面白屏 | 构建产物路径错 / 服务未起 | 观察器先做 HTTP 200 + console 检查，属 model 失败照常判分 |
| WebGL 初始化失败 | 无 GPU / 未启用 SwiftShader | 启动参数加 --use-gl=swiftshader；记录宿主 GPU |
| 交互用例 flaky | 元素可见性/动画竞争 | 用 expect(locator).toBeVisible + 固定时钟；同一用例重跑 3 次定级 |
| 端口冲突 | 多个 trial 共用固定端口 | 随机端口 + 健康检查重试；冲突判定 environment |
