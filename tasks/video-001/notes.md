# video-001 答案与验收说明

> 状态：v1 · 维护：tasks-b · 最后更新：2026-11-06
> 上游：[00-conventions](../../docs/00-conventions.md) · 相关：[video-ffmpeg playbook](../../docs/appendix/playbooks/video-ffmpeg.md)

## 1. 结构

    video-001/
    ├── task.yaml / instruction.md / persona.md / rubric.yaml / defects.yaml
    ├── canary.md / notes.md
    ├── starter/                  # Agent 可见：README 契约 + intro.py 半成品 + tests/
    ├── answers/{reference-a.patch, reference-b.patch, empty-agent.md}
    └── hidden-tests/
        ├── run.sh                # 完整验收（需 ffmpeg/ffprobe）或 --frames-only
        ├── probe.py              # ffprobe 硬门槛（含 --selftest 样例）
        ├── frames.py             # 抽帧结构检查（含 --selftest 合成用例）
        ├── plan_check.py         # 节拍表检查
        └── check_locks.py / locks.sha256

## 2. 环境

- 渲染机：python:3.12-slim + ffmpeg/ffprobe（版本固定；本机 macOS 无 ffmpeg）。
- 开发机本地可跑：单帧渲染、节拍表、帧结构检查（都不需要 ffmpeg）。
- 完整验收需要 ffmpeg：缺少时 run.sh 退出码 2（基础设施错误，换机重试，不计模型失败）。

## 3. 验证命令与实测证据（2026-11-06，本机）

    $ bash hidden-tests/run.sh --selftest
    video-001 selftest
      [OK]   probe.py 样例自检（退出码 0）
      [OK]   frames.py 合成用例自检（退出码 0）
      [OK]   reference-a 哈希锁（退出码 0）
      [OK]   reference-a 逐帧检查（退出码 0）
      [OK]   reference-b 哈希锁（退出码 0）
      [OK]   reference-b 逐帧检查（退出码 0）
      [OK]   starter（1280x720/24s/1 段）逐帧检查（退出码 1）
      [OK]   tests/ 被修改的哈希锁（退出码 3）
      [OK]   参考解 A 单帧确定性（退出码 0）
    selftest: 9 通过 / 0 失败
    SELFTEST PASS

  说明：本机没有 ffmpeg，因此 ffprobe 端到端用 --selftest 的内置样例验证检查逻辑；
  参考解 A/B 的真实 1920x1080 单帧与 6 帧结构检查已实跑通过。

## 4. 参考解差异

| 维度 | A（145 行 patch） | B（144 行 patch） |
| --- | --- | --- |
| 构图 | 左对齐 180px 边距 | 全部居中 |
| 节奏 | 五段 3/9/9/6/3 秒 | 六段 5 秒等分 |
| 配色 | 蓝紫主色 + 冷灰 | 蓝色主色 + 暖灰文字 |
| 编码 | libx264 crf18 preset medium | libx264 crf17 preset slow |

## 5. 已知容差

- 时长 30.0±0.1s；帧数必须 900（30fps）。
- 抽帧结构检查对颜色使用阈值与占比，不做像素级比对，避免编码色偏误伤。
- ffprobe 解析帧率字段用字符串 30/1（ffprobe 对 CFR 流稳定输出该形式）。
- TODO(run-on-host)：在装有 ffmpeg 的验证机执行 bash hidden-tests/run.sh 一次并归档输出。

## 6. 隔离声明与 Schema 校准

- task.yaml 已按 task.schema.json v1.0 编写；隔离三声明（agent_user/environment_mode/network_mode）
  暂放 extensions.isolation，待 Schema 增补 resources 字段后迁移，lint_extend_select 同理。
- 预算耗尽判 Failed 时标注 budget_exhausted（见 stop_conditions）。

## 7. 观测手段（给 Observer/裁判）

| 观察项 | 手段 | 命令 | 证据形式 |
| --- | --- | --- | --- |
| 容器/流指标 | ffprobe JSON | python3 hidden-tests/probe.py --input intro.mp4 | JSON（F2） |
| 固定时间点抽帧 | ffmpeg 抽帧 | ffmpeg -ss 5.0 -i intro.mp4 -frames:v 1 f.ppm | PPM 帧 |
| 视觉结构统计 | 标准库解析 PPM | python3 hidden-tests/frames.py --dir frames | 亮度/主色占比 |
| 节拍表 | --plan 输出解析 | python3 hidden-tests/plan_check.py <candidate> | 段数/连续性 |
| 确定性 | 同帧两次 sha256 | --render-frame 两次 + shasum | 哈希对比 |
| 抽帧给裁判 | 6 帧 contact sheet | ffmpeg -vf tile=3x2 | 图片证据 |
