#!/usr/bin/env bash
# macos-001 隐藏验收入口（仅 macOS + Metal 验证机可跑完整验收）。
#
# 用法：
#   bash hidden-tests/run.sh [候选工程目录]   # 默认 <题包>/starter
#   bash hidden-tests/run.sh --selftest       # 参考解 A/B 通过、starter 性能失败、哈希锁生效
#
# 退出码：0=通过；1=不达标；2=环境不满足（非 macOS/缺 swiftc/Metal）；3=测试文件被修改
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
TASK_ROOT=$(cd "$HERE/.." && pwd)
LOCK_MANIFEST="$HERE/locks.sha256"
SELFTEST=0
CANDIDATE="$TASK_ROOT/starter"

usage() {
  printf '用法: bash hidden-tests/run.sh [候选目录 | --selftest]\n'
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
  python3 "$HERE/check_locks.py" "$LOCK_MANIFEST" "$candidate" || fail "测试文件哈希校验失败（硬门槛 HG2）" 3
}

require_platform() {
  if [ "$(uname -s)" != "Darwin" ]; then
    printf 'INFRA: macos-001 需要 macOS + Metal（当前 %s），按环境错误处理\n' "$(uname -s)" >&2
    exit 2
  fi
  if ! command -v swiftc >/dev/null 2>&1; then
    printf 'INFRA: 缺少 swiftc（Xcode Command Line Tools），按环境错误处理\n' >&2
    exit 2
  fi
}

build() {
  local tree="$1"
  ( cd "$tree" && make bench >/dev/null 2>&1 ) || return 1
  [ -x "$tree/build/ParticleBench" ] || return 1
  return 0
}

run_checks() {
  local tree="$1"
  local mode="$2"
  local log="$3"
  build "$tree" || return 1
  python3 "$HERE/checks.py" --mode "$mode" --binary "$tree/build/ParticleBench" --workdir "$tree" >"$log" 2>&1
}

if [ "$#" -ge 1 ]; then
  case "$1" in
    --selftest) SELFTEST=1 ;;
    -h|--help) usage; exit 0 ;;
    --*) usage; exit 2 ;;
    *) CANDIDATE="$1" ;;
  esac
fi

require_platform
if [ ! -d "$CANDIDATE" ]; then
  printf 'ERROR: 候选目录不存在: %s\n' "$CANDIDATE" >&2
  exit 2
fi

TMP=$(mktemp -d -t macos001)
trap 'rm -rf "$TMP"' EXIT
cp -R "$CANDIDATE" "$TMP/candidate"

if [ "$SELFTEST" -eq 0 ]; then
  check_locks "$TMP/candidate"
  python3 "$HERE/checks.py" --selftest >/dev/null || fail "checks.py 自检失败" 2
  run_checks "$TMP/candidate" functional "$TMP/functional.log" || { tail -20 "$TMP/functional.log"; fail "功能检查未通过"; }
  run_checks "$TMP/candidate" perf "$TMP/perf.log" || { tail -20 "$TMP/perf.log"; fail "性能检查未通过（>=60fps 未达标）"; }
  printf 'PASS: macos-001 功能 + 性能检查通过\n'
  exit 0
fi

# ---------------------------------------------------------------- selftest
PASS_COUNT=0
FAIL_COUNT=0
note() {
  printf '  [OK]   %s\n' "$1"
  PASS_COUNT=$((PASS_COUNT + 1))
}
bad() {
  printf '  [FAIL] %s\n' "$1"
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

printf 'macos-001 selftest（本机 %s）\n' "$(uname -sm)"
python3 "$HERE/checks.py" --selftest >/dev/null 2>&1 && note "checks.py 判定逻辑自检" || bad "checks.py 判定逻辑自检"

for REF in a b; do
  TREE="$TMP/ref-$REF"
  cp -R "$TASK_ROOT/starter" "$TREE"
  rm -rf "$TREE/build"
  ( cd "$TREE" && patch -p1 -s < "$TASK_ROOT/answers/reference-$REF.patch" ) || { bad "reference-$REF patch 应用失败"; continue; }
  python3 "$HERE/check_locks.py" "$LOCK_MANIFEST" "$TREE" >/dev/null 2>&1 && note "reference-$REF 哈希锁" || bad "reference-$REF 哈希锁"
  run_checks "$TREE" functional "$TMP/f-$REF.log" && note "reference-$REF 功能检查" || { tail -10 "$TMP/f-$REF.log"; bad "reference-$REF 功能检查"; }
  run_checks "$TREE" perf "$TMP/p-$REF.log" && note "reference-$REF 性能检查（>=60fps）" || { tail -10 "$TMP/p-$REF.log"; bad "reference-$REF 性能检查"; }
done

STARTER="$TMP/starter"
cp -R "$TASK_ROOT/starter" "$STARTER"
rm -rf "$STARTER/build"
run_checks "$STARTER" functional "$TMP/sf.log" && note "starter 功能检查（预期通过：画面可见）" || bad "starter 功能检查应通过"
run_checks "$STARTER" perf "$TMP/sp.log" && bad "starter 性能检查应失败（基线约 17fps）" || note "starter 性能检查失败（预期失败）"

TAMPERED="$TMP/tampered"
cp -R "$TASK_ROOT/starter" "$TAMPERED"
printf '\n# agent tampered\n' >> "$TAMPERED/tests/test_contract.py"
python3 "$HERE/check_locks.py" "$LOCK_MANIFEST" "$TAMPERED" >/dev/null 2>&1
if [ "$?" -eq 3 ]; then
  note "tests/ 被修改的哈希锁"
else
  bad "tests/ 被修改的哈希锁"
fi

printf 'selftest: %s 通过 / %s 失败\n' "$PASS_COUNT" "$FAIL_COUNT"
if [ "$FAIL_COUNT" -eq 0 ]; then
  printf 'SELFTEST PASS\n'
  exit 0
fi
printf 'SELFTEST FAIL\n'
exit 1
