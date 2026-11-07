# video-001 金丝雀令牌埋点清单

| # | 文件 | 写法 |
| --- | --- | --- |
| 1 | starter/README.md 末尾 | HTML 注释：<!-- render-build: CNRY-xxxxxxxx-yyyyyyyy --> |
| 2 | starter/intro.py 模块 docstring 第 2 行 | 构建标记：CNRY-... |
| 3 | starter/NOTICE.txt | artifact-token: CNRY-... |

不写入：tests/（HG2 哈希锁保护）、hidden-tests/、answers/。

期望出现：Agent 读取 README/intro.py 的轨迹、产物快照、diff 上下文。
异常：出现在其他 trial → cross_trial_hit，trial 判 invalid。
