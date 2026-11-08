#!/usr/bin/env bash
# harmony-001 隐藏验收入口（验证侧执行，不进入 Agent 镜像）。
#
# 用法：
#   bash hidden-tests/run.sh [候选工程目录]     # 默认 <题包>/starter；退出码 0=通过
#   bash hidden-tests/run.sh --device          # 构建/签名/安装/启动（需要设备侧工具链）
#   bash hidden-tests/run.sh --selftest        # 参考解 A/B 通过、starter 失败、契约篡改被拦
#
# 检查分两段：
#   1) 静态：工程结构 / NAPI 接线 / 权限 / UI 调用（static_checks.py）
#   2) 数值：把候选的 C++ 内核当普通 C++ 编译并跑像素用例（需要宿主有 clang++/g++）
# 无 HarmonyOS 工具链时（本机即如此）第 2 段仍可完整执行，这是本包的主要可复现证据。
#
# 退出码：0=通过；1=隐藏测试失败；2=用法/环境错误；3=契约文件被修改（硬门槛）
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
TASK_ROOT=$(cd "$HERE/.." && pwd)
CANDIDATE="$TASK_ROOT/starter"
SELFTEST=0
DEVICE=0

usage() { printf '用法: bash hidden-tests/run.sh [候选工程目录 | --device | --selftest]\n'; }
fail() { printf 'FAIL: %s\n' "$1" >&2; exit "${2:-1}"; }

if [ "$#" -ge 1 ]; then
  case "$1" in
    --selftest) SELFTEST=1 ;;
    --device) DEVICE=1 ;;
    -h|--help) usage; exit 0 ;;
    --*) usage; exit 2 ;;
    *) CANDIDATE="$1" ;;
  esac
fi

run_numeric() {
  local cand="$1"
  local cpp_dir="$cand/entry/src/main/cpp"
  [ -d "$cpp_dir" ] || fail "候选缺少 C++ 目录：$cpp_dir" 2
  local cxx="${CXX:-}"
  if [ -z "$cxx" ]; then
    if command -v clang++ >/dev/null 2>&1; then cxx=clang++
    elif command -v g++ >/dev/null 2>&1; then cxx=g++
    else fail "宿主没有 C++ 编译器（需要 clang++ 或 g++）" 2; fi
  fi
  local sources=()
  while IFS= read -r f; do sources+=("$f"); done < <(find "$cpp_dir" -maxdepth 1 -name '*.cpp' ! -name 'napi_init.cpp' | sort)
  [ "${#sources[@]}" -ge 1 ] || fail "候选没有可编译的 C++ 内核源文件" 2
  local out
  out=$(mktemp "${TMPDIR:-/tmp}/harmony001_imgops.XXXXXX")
  "$cxx" -std=c++17 -O2 -Wall -Wextra -I "$cpp_dir" \
    "$HERE/tests/test_image_ops.cpp" "${sources[@]}" -o "$out" || fail "候选 C++ 内核编译失败" 1
  "$out"
  local code=$?
  rm -f "$out"
  return $code
}

run_device() {
  # TODO(run-on-host): 本段依赖 HarmonyOS 命令行工具链（hvigorw/ohpm/hdc）与真机，
  # 需在设备侧执行器上跑一次并把 hilog 归档为证据（归档路径见 notes.md §5）。
  local cand="$1"
  for tool in ohpm hvigorw hdc; do
    command -v "$tool" >/dev/null 2>&1 || fail "缺少 HarmonyOS 命令行工具：$tool（仅设备侧执行器具备）" 2
  done
  ( cd "$cand" && ohpm install --all ) || fail "ohpm install 失败" 1
  ( cd "$cand" && hvigorw clean --no-daemon ) || fail "hvigorw clean 失败" 1
  ( cd "$cand" && hvigorw assembleHap --mode module -p product=default -p buildMode=debug --no-daemon ) || fail "HAP 构建失败" 1
  local hap
  hap=$(ls "$cand"/entry/build/default/outputs/default/*-signed.hap 2>/dev/null | head -n 1)
  [ -n "$hap" ] || fail "构建目录里没有签名 HAP" 1
  hdc list targets | grep -q . || fail "设备池里没有在线设备" 2
  hdc install -r "$hap" || fail "hdc install 失败" 1
  hdc shell aa start -a EntryAbility -b com.example.imagefilters || fail "应用启动命令失败" 1
  sleep 10
  local log
  log=$(mktemp "${TMPDIR:-/tmp}/harmony001_hilog.XXXXXX")
  hdc shell hilog -T imagefilters -t app > "$log" 2>&1 || true
  if grep -E 'CppCrash|AppRecover|SIGSEGV|Abort' "$log" >/dev/null 2>&1; then
    tail -n 30 "$log"; rm -f "$log"; fail "启动后 10 秒内检测到崩溃日志" 1
  fi
  rm -f "$log"
  printf 'PASS: 构建/签名/安装/启动成功\n'
}

if [ "$DEVICE" -eq 1 ]; then
  run_device "$CANDIDATE"
  exit 0
fi

if [ "$SELFTEST" -eq 0 ]; then
  python3 "$HERE/static_checks.py" "$CANDIDATE"
  SC=$?
  if [ "$SC" -eq 3 ]; then fail "契约文件被修改（硬门槛 HG3）" 3; fi
  if [ "$SC" -ne 0 ]; then fail "静态检查未通过" 1; fi
  run_numeric "$CANDIDATE" || fail "C++ 内核数值测试未通过" 1
  printf 'PASS: harmony-001 隐藏验收通过（静态 + 数值）\n'
  exit 0
fi

TMP=$(mktemp -d "${TMPDIR:-/tmp}/harmony001_self.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
PASS=0; FAILED=0

expect_pass() {
  local label="$1" tree="$2"
  if python3 "$HERE/static_checks.py" "$tree" >/dev/null 2>&1 && run_numeric "$tree" >/dev/null 2>&1; then
    printf '  [OK]   %s：静态+数值通过（预期通过）\n' "$label"; PASS=$((PASS+1))
  else
    printf '  [FAIL] %s：预期通过但失败\n' "$label"
    python3 "$HERE/static_checks.py" "$tree" 2>&1 | tail -n 5
    run_numeric "$tree" 2>&1 | tail -n 5
    FAILED=$((FAILED+1))
  fi
}

printf 'harmony-001 selftest\n'
for REF in a b; do
  TREE="$TMP/ref-$REF"
  cp -R "$TASK_ROOT/starter" "$TREE"
  ( cd "$TREE" && patch -p1 -s < "$TASK_ROOT/answers/reference-$REF.patch" ) || fail "参考解 $REF patch 应用失败" 2
  expect_pass "reference-$REF" "$TREE"
done

TREE="$TMP/starter"
cp -R "$TASK_ROOT/starter" "$TREE"
if python3 "$HERE/static_checks.py" "$TREE" >/dev/null 2>&1; then
  printf '  [FAIL] starter：静态检查意外通过\n'; FAILED=$((FAILED+1))
else
  printf '  [OK]   starter：静态检查失败（预期失败）\n'; PASS=$((PASS+1))
fi
if run_numeric "$TREE" >/dev/null 2>&1; then
  printf '  [FAIL] starter：数值测试意外通过\n'; FAILED=$((FAILED+1))
else
  printf '  [OK]   starter：数值测试失败（预期失败）\n'; PASS=$((PASS+1))
fi

TAMPERED="$TMP/tampered"
cp -R "$TASK_ROOT/starter" "$TAMPERED"
printf '\n// tampered\n' >> "$TAMPERED/entry/src/main/cpp/image_ops.h"
python3 "$HERE/static_checks.py" "$TAMPERED" >/dev/null 2>&1
if [ "$?" -eq 3 ]; then
  printf '  [OK]   契约文件被修改：哈希校验拦截（预期拦截）\n'; PASS=$((PASS+1))
else
  printf '  [FAIL] 契约文件被修改：未拦截\n'; FAILED=$((FAILED+1))
fi

printf 'selftest: %s 通过 / %s 失败\n' "$PASS" "$FAILED"
[ "$FAILED" -eq 0 ] && { printf 'SELFTEST PASS\n'; exit 0; }
printf 'SELFTEST FAIL\n'; exit 1
