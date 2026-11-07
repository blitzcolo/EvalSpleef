# intro-render：30 秒片头生成器

用 Python 标准库逐帧生成画面，通过管道交给系统 ffmpeg 编码为 MP4。当前是半成品，参数和内容都不符合需求。

## 需求（验收契约）

| 项 | 要求 |
| --- | --- |
| 文件 | H.264 编码的 MP4（容器 mp4），单视频流，无音频 |
| 时长 | 30.0 秒，允许 ±0.1 秒（共 900 帧 @ 30fps） |
| 分辨率 | 1920x1080 |
| 帧率 | 30 fps（avg_frame_rate=30/1，r_frame_rate=30/1） |
| 像素格式 | yuv420p |
| 编码 | libx264，crf ≤ 20，-movflags +faststart |
| 画面 | 深色背景 + 品牌主色强调条，五段结构：0-3 秒 logo 揭示、3-12 秒主标题、12-21 秒三张特性卡、21-27 秒色带扫过、27-30 秒结束卡 |
| 确定性 | 同参数两次生成，抽帧 PPM 的 sha256 必须一致（不依赖随机数） |
| 本地自检 | python3 intro.py --render-frame 5 --out /tmp/f.ppm 输出 1920x1080 P6 PPM |

## 运行

    # 完整渲染（需要系统 ffmpeg；当前开发机没有）
    python3 intro.py --out intro.mp4
    # 只渲染单帧（无 ffmpeg 也能跑，用于本地检查构图）
    python3 intro.py --render-frame 5.0 --out /tmp/frame.ppm
    # 打印场景节拍表
    python3 intro.py --plan

## 约定

- 只用标准库；不读取网络、不读外部素材。
- 画面完全由代码决定（字体用内置 5x7 点阵，不允许读取系统字体）。
- 保留 --out / --render-frame / --plan 三个入口，语义不变。

## 已知问题

- 当前导出参数是 1280x720 / 25fps / mpeg4，与契约不符。
- 场景只有一段静态标题，缺 logo 揭示、特性卡、色带与结束卡。
- 颜色与排版偏随意，主色与强调条的对比度不够。
