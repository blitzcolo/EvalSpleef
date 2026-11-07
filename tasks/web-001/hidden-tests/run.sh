#!/usr/bin/env bash
# web-001 隐藏验收入口（验证侧执行）。
# 环境具备 @playwright/test 与 Chromium 时运行完整交互验收；
# 否则做静态契约检查并明确 SKIP（退出码仍为 0，避免把「环境缺依赖」误判成题目失败）。
set -euo pipefail
cd "$(dirname "$0")"

APP_DIR="${APP_DIR:-$(cd .. && pwd)/workspace}"
export APP_DIR

if node -e "require.resolve('@playwright/test')" >/dev/null 2>&1; then
  npx playwright test --config=playwright.config.js
  exit 0
fi

# TODO(run-on-host): 在预装 @playwright/test 与 Chromium 的验证镜像内跑完整用例
python3 - <<'PY'
import json
import sys
from pathlib import Path

here = Path(".").resolve()
required = ["kanban.spec.js", "playwright.config.js"]
missing = [name for name in required if not (here / name).is_file()]
spec = (here / "kanban.spec.js").read_text(encoding="utf-8")
expected_tests = [f"T{i} " for i in range(1, 11)]
contract = [
    "data-card-id", "card-count-", "empty-state-", "theme-toggle",
    "data-theme", "ArrowRight", "localStorage", "page.mouse",
]
checks = {
    "files_present": not missing,
    "test_count_10": all(token in spec for token in expected_tests),
    "contract_tokens": all(token in spec for token in contract),
}
print(json.dumps({"static_checks": checks, "missing": missing}, ensure_ascii=False))
sys.exit(0 if all(checks.values()) else 1)
PY
echo "SKIP: 当前环境未安装 @playwright/test；已通过静态契约检查，完整交互验证 TODO(run-on-host)。"
