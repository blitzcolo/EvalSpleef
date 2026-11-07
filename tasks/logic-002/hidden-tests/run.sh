#!/usr/bin/env bash
# logic-002 隐藏验收入口（验证侧执行；仅用 python3 标准库，不安装依赖）
# 退出码：0 = 自检通过；设置 CANDIDATE_ANSWER 时还要求提交验收三闸全过。
set -euo pipefail
cd "$(dirname "$0")"

python3 -m unittest discover -s . -p 'test_*.py' -v

if [[ -n "${CANDIDATE_ANSWER:-}" ]]; then
  python3 verify_submission.py --answer "${CANDIDATE_ANSWER:-}" --json
fi
