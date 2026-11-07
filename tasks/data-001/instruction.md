# 帮我把这批脏日志洗干净，出个月报口径

我们小团队的访问日志攒了半年的，格式是祖传的：一部分是「时间 IP 方法 路径 状态码 耗时」这种空格分隔，一部分是 Apache 老格式，还有一部分是后来加的 JSON 行。中间还混着轮转标记、截断行、重复请求、明显错误的字段。

现在要做一份固定口径的月报，我需要你**交一个脚本**给我，我以后每月自己拿来跑。工作区里有：

- `access.log`：我挑出来的样例（50 行，3 种格式 + 5 类脏行都有）；
- `expected_sample.json`：这批样例的**正确报告**，你可以拿来自检——脚本跑出来的结果要跟它对得上。

## 1. 你要交什么

在 `/workspace/clean_logs.py` 写一个 Python 脚本，接口固定：

```bash
python3 clean_logs.py <input.log> <output.json>
```

- 只读 `input.log`、只写 `output.json`，不要改输入文件；
- 验收时我会拿**另一批同样口径的脏日志**跑你的脚本（内容不同，规则相同），所以别把样例的统计数字写死；
- 标准库就够；我这边也装了 pandas，你想用也行。

## 2. 日志行的三种合法格式

1. 简文本：`时间 IP 方法 路径 状态码 耗时`，可带 `rid=xxx`
   例：`2026-11-03T02:54:04Z 172.16.0.5 POST /api/search 404 936ms rid=r-83415`
2. Apache：`IP - - [dd/Mon/yyyy:HH:MM:SS +0000] "方法 路径 HTTP/1.1" 状态码 耗时`，可带 `rid=xxx`
   例：`10.0.0.7 - - [04/Nov/2026:06:23:00 +0000] "PUT /api/login HTTP/1.1" 302 119ms`
3. JSON：一行一个对象，必含 `ts/client/method/path/status/dur_ms`，可选 `rid`
   例：`{"ts":"2026-11-03T21:01:32Z","client":"10.0.1.8","method":"GET","path":"/api/products","status":503,"dur_ms":507.8}`

字段约定：时间是 UTC；方法只能是 GET/POST/PUT/DELETE；路径以 `/` 开头（不做去查询参数等加工，原样使用）；状态码必须 100-599；耗时单位 ms 或 s（`936ms`、`0.395s` 都合法，统一换算成毫秒）。

## 3. 脏行分类（一条脏行只归一类，按下面的顺序判定）

| 原因 | 判定 |
| --- | --- |
| `bad_format` | 不匹配上面任何一种格式；或匹配了格式但 IP/方法/路径非法 |
| `bad_timestamp` | 结构匹配、但时间无法解析 |
| `bad_status` | 时间可解析、但状态码不是 100-599 的整数 |
| `bad_duration` | 时间/状态码都可解析、但耗时不是合法的 `数字ms` 或 `数字s` |
| `duplicate_rid` | 以上都通过、但该 `rid` 前面已经出现过（第一次出现的那行算有效） |

空行也算 `bad_format`。

## 4. 报告口径

```json
{
  "total_valid": 39,
  "malformed": 11,
  "malformed_by_reason": {"bad_format": 1, "bad_timestamp": 2, "bad_status": 1, "bad_duration": 4, "duplicate_rid": 3},
  "unique_clients": 8,
  "error_rate": 0.4615,
  "p95_duration_ms": 936.0,
  "top_paths": [{"path": "/api/health", "count": 10}, {"path": "/api/cart", "count": 7}, {"path": "/api/orders", "count": 6}, {"path": "/api/search", "count": 6}, {"path": "/api/login", "count": 4}]
}
```

- `total_valid`：有效行数；`malformed`：脏行总数；`malformed_by_reason` 必须五个键齐全（没有就填 0）；
- `unique_clients`：有效行里不同 IP 数；
- `error_rate`：有效行中状态码 ≥500 的比例，保留 4 位小数；
- `p95_duration_ms`：有效行耗时（毫秒）按 nearest-rank 法取第 `ceil(0.95 × N)` 小值（升序第几个，1 始），保留 1 位小数；
- `top_paths`：按次数降序、同次数按路径字典序升序，取前 5（不足 5 个就全列）。

## 5. 验收与容差

我会在你的脚本输出上比对：计数类字段必须完全相等；`error_rate` 允许 ±0.0001；`p95_duration_ms` 允许 ±1.0 ms。格式（纯 JSON、键集合、排序）也要对。样例上跑出的报告必须和 `expected_sample.json` 一致。

先把样例跑通，再注意代码别写死数字。弄好了跟我说一声。