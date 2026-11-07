# 30 秒片头动画导出 MP4

我们市场部要做一支 30 秒的年度发布会片头，动画部分要**用代码生成**（不手工剪辑、不用外部素材）。组里已经有一个半成品生成器 **intro.py**，README.md 写清了验收契约，但现在的导出参数和画面内容都对不上。

请把生成器改到符合 README.md 契约，并导出一份 **intro.mp4** 放在工作目录下。要求：

1. 以 **README.md 的验收契约**为准：时长、分辨率、帧率、编码、像素格式、画面结构都要符合；不要修改 README.md。
2. 画面必须由代码逐帧生成（内置点阵字体，允许使用系统 ffmpeg 做编码）。
3. 不要修改 **tests/** 下的文件（验收会核对测试文件哈希）。
4. 只用 Python 标准库 + 系统 ffmpeg；沙箱无网络。
5. 保持 **--out / --render-frame / --plan** 三个入口语义不变，方便我们本地检查单帧。
6. 自测建议：

       python3 intro.py --render-frame 5 --out /tmp/frame.ppm   # 无 ffmpeg 也能跑
       python3 intro.py --plan
       python3 intro.py --out intro.mp4                          # 有 ffmpeg 时完整导出

完成后说明你的五段结构与配色思路。
