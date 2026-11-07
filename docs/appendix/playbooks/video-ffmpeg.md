# Playbook · 视频 / 动画生成（ffmpeg）

> 状态：v1 · 维护：tasks-b · 最后更新：2026-11-06
> 上游：[00-conventions](../../docs/00-conventions.md) · 相关：[02-eval-flow](../../docs/02-eval-flow.md)、[04-isolation](../../docs/04-isolation.md)、[06-answer-and-acceptance](../../docs/06-answer-and-acceptance.md)

面向赛道：video（代码生成动画、导出 MP4、时长/分辨率/编码硬门槛）。示例题包：[video-001](../examples/video-001/task.yaml)。

## 1. 环境镜像要点

| 项 | 建议 | 说明 |
| --- | --- | --- |
| 基础镜像 | python:3.12-slim + ffmpeg/ffprobe 固定版本 | 版本写进 batch 记录；ffprobe 字段随版本变化 |
| 编码器 | libx264（-c:v libx264）；x264 版本随 ffmpeg 固定 | 硬门槛只认 h264/yuv420p；不用 GPU 编码 |
| 字体 | 若题目允许文字，预装 Noto CJK；否则要求内置点阵字体 | 外部字体不可用，避免环境差异 |
| 资源 | 4 vCPU / 4 GiB / 4 GiB 磁盘（1080p 900 帧约 1-2 分钟） | 输出文件大小设上限（如 200MB） |
| 网络 | 断网；不许下载素材/BGM | 防止用外部素材绕过「代码生成」 |
| 渲染确定性 | 禁随机、禁系统时间；同一输入两次 PPM sha256 一致 | 随机性会让回归比对失效 |

## 2. 构建与运行命令

     # 逐帧生成 + 管道编码（Python 标准库示例）
     python3 intro.py --out intro.mp4
     # 等价 ffmpeg 关键参数
     ffmpeg -y -f rawvideo -pix_fmt rgb24 -s 1920x1080 -r 30 -i - -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p -movflags +faststart intro.mp4
     # 单帧调试（无 ffmpeg 也能跑，用于构图自检）
     python3 intro.py --render-frame 5.0 --out /tmp/frame.ppm
     # 元数据检查（[ffprobe 文档](https://ffmpeg.org/ffprobe.html)）
     ffprobe -v error -print_format json -show_format -show_streams intro.mp4

编码参数口径：crf 与 preset 写进题目契约；硬门槛看 codec/pix_fmt/分辨率/帧率/时长，不看 crf 数值；H.264 编码建议见 [FFmpeg Encode/H.264](https://trac.ffmpeg.org/wiki/Encode/H.264)。

## 3. 观察手段清单（供 Observer 直接引用）

| 观察项 | 手段 | 命令 | 证据形式 |
| --- | --- | --- | --- |
| 容器/流信息 | ffprobe JSON | ffprobe -v error -print_format json -show_format -show_streams | JSON（F2 素材） |
| 时长/帧数 | format.duration、nb_frames | 同上 | 数值断言 |
| 分辨率/帧率 | width/height/avg_frame_rate | 同上 | 数值断言 |
| 编码/像素格式 | codec_name、pix_fmt | 同上 | 枚举断言 |
| 固定时间点抽帧 | ffmpeg -ss 抽单帧 | ffmpeg -v error -ss 5.0 -i intro.mp4 -frames:v 1 -f image2 f.ppm | PPM + 结构统计 |
| 抽帧给裁判 | 6 个时间点拼 contact sheet | ffmpeg -i intro.mp4 -vf tile=3x2 -frames:v 1 sheet.png | 图片证据 |
| 视觉结构 | 标准库解析 PPM：亮度/主色占比/非背景像素 | python3 hidden-tests/frames.py --dir frames | JSON 统计 |
| 确定性 | 两次渲染同帧 sha256 | shasum -a 256 a.ppm b.ppm | 哈希对比 |
| 音频流 | 不应出现 codec_type=audio | ffprobe 同上 | 断言 |
| 文件完整性 | 快速解码全片 | ffmpeg -v error -i intro.mp4 -f null - | 退出码 |

抽帧时间点固定写进隐藏测试（例如 1/5/12/20/26/29 秒），不临时决定。

## 4. 验收命令

     # 完整验收（需要 ffmpeg/ffprobe；缺失时按 environment 处理）
     bash hidden-tests/run.sh
     # 自检：参考解 A/B 通过、starter 失败、检查器合成用例正确、哈希锁生效
     bash hidden-tests/run.sh --selftest
     # 仅逐帧结构检查（无 ffmpeg 的开发机可用）
     bash hidden-tests/run.sh --frames-only
     # 失败重判
     python3 scripts/regrade.py --trial <trial_id> --reason verification_flake

硬门槛建议：ffprobe 全指标达标 + 抽帧结构检查 + 渲染确定性；任一失败即 Failed，不做差不多放行。

## 5. 隔离要点

1. 每 trial 独立容器；输出 MP4 只从声明的 artifacts 路径导出，限制大小与数量。
2. 不共享 ffmpeg 二进制之外的素材目录；禁止 mount 宿主媒体库。
3. 验证在独立沙箱：Agent 产物只拷 MP4 与源码，不拷渲染中间帧（防止夹带）。
4. 输出确定性要求写进契约，避免用系统时间/随机数；评审看抽帧，不看原始工程。
5. 完整验收机必须装 ffmpeg：缺失是基础设施错误（换机重试），不是模型失败。

## 6. 常见故障与处置

| 现象 | 根因 | 处置 |
| --- | --- | --- |
| 缺少 ffmpeg/ffprobe | 镜像未装或 PATH 不对 | 镜像固定版本；缺失判 environment/retry |
| 时长差 0.1s | 帧数取整/编码器补齐 | 契约给 ±0.1s 容差；帧数用 -frames:v 锁定 |
| pix_fmt 不是 yuv420p | 编码器默认选择 | 显式 -pix_fmt yuv420p；检查 filter 链输出格式 |
| 抽帧全黑 | seek 太靠前/关键帧问题 | -ss 放在 -i 之后做精确 seek；抽帧前先 ffprobe 校验时长 |
| 同参数两次输出不同 | 画面含随机/时间 | 固定种子与帧序号驱动；禁用系统时间 API |
| 编码超时 | preset 过慢/分辨率高 | 契约固定 preset；观察阶段超时单独计（不混入 model 超时） |
| 磁盘写满 | 中间帧/日志未清理 | 渲染走管道不落中间帧；限制输出大小并清理临时目录 |
