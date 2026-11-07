# Gateway over real HTTP (success criterion 4)

Captured 2026-11-07T19:52:12.638Z against a real `spleef-gateway` process (data port 18390, admin port 18391) in front of the in-repo fake OpenAI server. Secrets are redacted. The upstream is a test double: not verified against a real vendor.

Scenarios: (a) chat with a trial key plus usage record, (b) over-budget key gets 429 cost_cap, (c) revoked key gets 401, (d) Anthropic Messages with a tool round-trips.

```text
===== PUT route
HTTP/1.1 200 OK
date: Sat, 07 Nov 2026 19:51:57 GMT
server: uvicorn
content-length: 237
content-type: application/json

{"name":"ep_demo","route":{"api_base":"http://127.0.0.1:58448/v1","model":"fake-model","protocol":"chat_completions","api_key_ref":"env://UPSTREAM_KEY","default_params":{},"extra_headers":{},"provider_lock":null,"cold_start_window_s":0}}

===== PUT price
HTTP/1.1 200 OK
date: Sat, 07 Nov 2026 19:51:57 GMT
server: uvicorn
content-length: 136
content-type: application/json

{"name":"ep_demo","price":{"input_per_mtok":1000.0,"output_per_mtok":2000.0,"cached_per_mtok":0.0,"basis":"api","table_version":"qa-1"}}

===== POST /admin/keys qa.trial.t01
HTTP/1.1 201 Created
date: Sat, 07 Nov 2026 19:51:57 GMT
server: uvicorn
content-length: 213
content-type: application/json

{"key_id":"tk_c94ffd61dea84499","secret":"<redacted>","trial_id":"qa.trial.t01","endpoint_id":"demo","max_budget_usd":5.0,"max_tokens":100000,"expires_at":"2026-11-07T20:51:57.854Z"}

===== POST /v1/chat/completions
HTTP/1.1 200 OK
date: Sat, 07 Nov 2026 19:51:57 GMT
server: uvicorn
content-length: 256
content-type: application/json
x-spleef-gateway-version: 0.1.0

{"id":"chatcmpl-1","object":"chat.completion","created":1794081117,"model":"fake-model","choices":[{"index":0,"message":{"role":"assistant","content":"fake reply"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}

===== GET /admin/usage
HTTP/1.1 200 OK
date: Sat, 07 Nov 2026 19:51:57 GMT
server: uvicorn
content-length: 272
content-type: application/json

[{"ts":"2026-11-07T19:51:57.884133+00:00","trial_id":"qa.trial.t01","round":0,"endpoint_id":"demo","upstream_model":"fake-model","input_tokens":1,"output_tokens":2,"cached_tokens":0,"reasoning_tokens":0,"latency_ms":2,"status_code":200,"cold_start_ms":0,"cost_usd":0.005}]

===== POST /admin/keys qa.trial.t02
HTTP/1.1 201 Created
date: Sat, 07 Nov 2026 19:51:57 GMT
server: uvicorn
content-length: 215
content-type: application/json

{"key_id":"tk_2ae9bc520c801b8e","secret":"<redacted>","trial_id":"qa.trial.t02","endpoint_id":"demo","max_budget_usd":1e-05,"max_tokens":100000,"expires_at":"2026-11-07T20:51:57.854Z"}

===== POST /v1/chat/completions
HTTP/1.1 429 Too Many Requests
date: Sat, 07 Nov 2026 19:51:57 GMT
server: uvicorn
content-length: 118
content-type: application/json
x-spleef-gateway-version: 0.1.0

{"error":{"message":"trial key budget exhausted (cost_cap)","type":"rate_limit_error","param":null,"code":"cost_cap"}}

===== POST /v1/chat/completions
HTTP/1.1 429 Too Many Requests
date: Sat, 07 Nov 2026 19:51:57 GMT
server: uvicorn
content-length: 118
content-type: application/json
x-spleef-gateway-version: 0.1.0

{"error":{"message":"trial key budget exhausted (cost_cap)","type":"rate_limit_error","param":null,"code":"cost_cap"}}

===== DELETE key1
HTTP/1.1 204 No Content
date: Sat, 07 Nov 2026 19:51:57 GMT
server: uvicorn

===== POST /v1/chat/completions
HTTP/1.1 401 Unauthorized
date: Sat, 07 Nov 2026 19:51:57 GMT
server: uvicorn
content-length: 134
content-type: application/json
x-spleef-gateway-version: 0.1.0

{"error":{"message":"unknown, revoked or expired trial key","type":"authentication_error","param":null,"code":"authentication_error"}}

===== POST /admin/keys qa.trial.t03
HTTP/1.1 201 Created
date: Sat, 07 Nov 2026 19:51:57 GMT
server: uvicorn
content-length: 213
content-type: application/json

{"key_id":"tk_da121f91f4b168f6","secret":"<redacted>","trial_id":"qa.trial.t03","endpoint_id":"demo","max_budget_usd":5.0,"max_tokens":100000,"expires_at":"2026-11-07T20:51:57.854Z"}

===== POST /v1/messages
HTTP/1.1 200 OK
date: Sat, 07 Nov 2026 19:51:57 GMT
server: uvicorn
content-length: 307
content-type: application/json
x-spleef-gateway-version: 0.1.0

{"id":"chatcmpl-2","type":"message","role":"assistant","model":"ep_demo","content":[{"type":"tool_use","id":"call_0","name":"get_time","input":{"tz":"x"}}],"stop_reason":"tool_use","stop_sequence":null,"usage":{"input_tokens":3,"output_tokens":2,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}

===== POST /v1/messages
HTTP/1.1 200 OK
date: Sat, 07 Nov 2026 19:51:57 GMT
server: uvicorn
content-length: 275
content-type: application/json
x-spleef-gateway-version: 0.1.0

{"id":"chatcmpl-3","type":"message","role":"assistant","model":"ep_demo","content":[{"type":"text","text":"tool-result:1"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":4,"output_tokens":1,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}
```
