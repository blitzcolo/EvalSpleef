#!/usr/bin/env bash
# debug-001 隐藏验收入口（验证侧执行，不进入 Agent 镜像）。
#
# 用法：
#   bash hidden-tests/run.sh [候选工程目录]   # 默认 <题包>/starter；退出码 0=通过
#   bash hidden-tests/run.sh --selftest       # 验证测试自身：参考解通过 / starter 失败 /
#                                             # 哈希锁生效 / 判分幂等 / 变异体全杀死
#
# 退出码：0=通过；1=隐藏测试失败；2=用法或环境错误；3=测试文件被修改（硬门槛 H2）
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
TASK_ROOT=$(cd "$HERE/.." && pwd)
LOCK_MANIFEST="$HERE/locks.sha256"
TESTS_DIR="$HERE/tests"
SELFTEST=0
CANDIDATE="$TASK_ROOT/starter"

usage() {
  printf '用法: bash hidden-tests/run.sh [候选工程目录 | --selftest]\n'
}

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  if [ "$#" -ge 2 ]; then
    exit "$2"
  fi
  exit 1
}

check_locks() {
  local candidate="$1"
  python3 "$HERE/check_locks.py" "$LOCK_MANIFEST" "$candidate" || fail "测试文件哈希校验失败（硬门槛 H2：tests/ 必须保持原样）" 3
}

run_suite() {
  local candidate="$1"
  local log="$2"
  PYTHONDONTWRITEBYTECODE=1 LEDGER_CANDIDATE="$candidate" \
    python3 -m unittest discover -s "$TESTS_DIR" -p 'test_*.py' -v >"$log" 2>&1
}

apply_patch() {
  local tree="$1"
  local patch_file="$2"
  ( cd "$tree" && patch -p1 -s < "$patch_file" )
}

if [ "$#" -ge 1 ]; then
  case "$1" in
    --selftest) SELFTEST=1 ;;
    -h|--help) usage; exit 0 ;;
    --*) usage; exit 2 ;;
    *) CANDIDATE="$1" ;;
  esac
fi

if [ ! -d "$CANDIDATE" ]; then
  printf 'ERROR: 候选工程目录不存在: %s\n' "$CANDIDATE" >&2
  exit 2
fi
if [ ! -d "$TESTS_DIR" ]; then
  printf 'ERROR: 缺少隐藏测试目录: %s\n' "$TESTS_DIR" >&2
  exit 2
fi

if [ "$SELFTEST" -eq 0 ]; then
  check_locks "$CANDIDATE"
  LOG=$(mktemp -t debug001_run)
  run_suite "$CANDIDATE" "$LOG"
  CODE=$?
  if [ "$CODE" -eq 0 ]; then
    printf 'PASS: debug-001 隐藏回归测试全部通过（22 项）\n'
    rm -f "$LOG"
    exit 0
  fi
  tail -n 40 "$LOG"
  rm -f "$LOG"
  printf 'FAIL: debug-001 隐藏回归测试未通过（退出码 %s）\n' "$CODE"
  exit 1
fi

# ---- selftest：证明验收脚本本身可靠 ----
TMP=$(mktemp -d -t debug001_self)
trap 'rm -rf "$TMP"' EXIT
PASS_COUNT=0
FAIL_COUNT=0

expect_pass() {
  local label="$1"
  local tree="$2"
  local log="$TMP/log.txt"
  python3 "$HERE/check_locks.py" "$LOCK_MANIFEST" "$tree" >/dev/null 2>&1
  local lock_code=$?
  run_suite "$tree" "$log"
  local code=$?
  if [ "$lock_code" -eq 0 ] && [ "$code" -eq 0 ]; then
    printf '  [OK]   %s：隐藏测试通过（预期通过）\n' "$label"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    printf '  [FAIL] %s：预期通过但失败（lock=%s suite=%s）\n' "$label" "$lock_code" "$code"
    tail -n 20 "$log"
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

expect_fail() {
  local label="$1"
  local tree="$2"
  local log="$TMP/log.txt"
  run_suite "$tree" "$log"
  local code=$?
  if [ "$code" -ne 0 ]; then
    printf '  [OK]   %s：隐藏测试失败（预期失败）\n' "$label"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    printf '  [FAIL] %s：预期失败但通过\n' "$label"
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

expect_lock_violation() {
  local label="$1"
  local tree="$2"
  python3 "$HERE/check_locks.py" "$LOCK_MANIFEST" "$tree" >/dev/null 2>&1
  local code=$?
  if [ "$code" -eq 3 ]; then
    printf '  [OK]   %s：哈希锁拦截（预期拦截）\n' "$label"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    printf '  [FAIL] %s：哈希锁未拦截（退出码 %s）\n' "$label" "$code"
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

printf 'debug-001 selftest\n'

for REF in a b; do
  TREE="$TMP/ref-$REF"
  cp -R "$TASK_ROOT/starter" "$TREE"
  apply_patch "$TREE" "$TASK_ROOT/answers/reference-$REF.patch" || fail "参考解 $REF patch 应用失败" 2
  expect_pass "reference-$REF" "$TREE"
done

expect_fail "starter（未修复）" "$TASK_ROOT/starter"

TAMPERED="$TMP/tampered"
cp -R "$TASK_ROOT/starter" "$TAMPERED"
printf '\n# agent tampered\n' >> "$TAMPERED/tests/test_public.py"
expect_lock_violation "tests/ 被修改" "$TAMPERED"

FIXED="$TMP/ref-a"
LOG1="$TMP/idem1.txt"
LOG2="$TMP/idem2.txt"
run_suite "$FIXED" "$LOG1"
C1=$?
run_suite "$FIXED" "$LOG2"
C2=$?
if [ "$C1" -eq 0 ] && [ "$C2" -eq 0 ]; then
  printf '  [OK]   判分幂等：同一产物连跑两次均通过\n'
  PASS_COUNT=$((PASS_COUNT + 1))
else
  printf '  [FAIL] 判分幂等：两次运行结果不一致\n'
  FAIL_COUNT=$((FAIL_COUNT + 1))
fi

if python3 "$HERE/mutate.py" "$FIXED" >"$TMP/mutation.log" 2>&1; then
  printf '  [OK]   变异测试：5/5 变异体被隐藏测试杀死\n'
  PASS_COUNT=$((PASS_COUNT + 1))
else
  printf '  [FAIL] 变异测试：存在存活变异体\n'
  tail -n 20 "$TMP/mutation.log"
  FAIL_COUNT=$((FAIL_COUNT + 1))
fi

printf 'selftest: %s 通过 / %s 失败\n' "$PASS_COUNT" "$FAIL_COUNT"
if [ "$FAIL_COUNT" -eq 0 ]; then
  printf 'SELFTEST PASS\n'
  exit 0
fi
printf 'SELFTEST FAIL\n'
exit 1
