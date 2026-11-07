#!/usr/bin/env bash
# vulkan-001 隐藏验收入口（验证侧执行，不进入 Agent 镜像）。
#
# 用法：
#   bash hidden-tests/run.sh [候选工程目录]        # 静态 + CPU 数学 + 截图比对（若参考图存在）
#   bash hidden-tests/run.sh --screenshot <ppm>    # 指定候选截图
#   bash hidden-tests/run.sh --gpu                 # 构建/运行/截图（需要 Vulkan SDK + GPU）
#   bash hidden-tests/run.sh --selftest            # 参考解 A/B 通过、starter 失败、契约篡改被拦
#
# 退出码：0=通过；1=隐藏测试失败；2=用法/环境错误；3=契约文件被修改（硬门槛）
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
TASK_ROOT=$(cd "$HERE/.." && pwd)
CANDIDATE="$TASK_ROOT/starter"
SHOT=""
SELFTEST=0
GPU=0

usage() { printf '用法: bash hidden-tests/run.sh [候选目录 | --screenshot FILE | --gpu | --selftest]\n'; }
fail() { printf 'FAIL: %s\n' "$1" >&2; exit "${2:-1}"; }

while [ "$#" -ge 1 ]; do
  case "$1" in
    --selftest) SELFTEST=1; shift ;;
    --gpu) GPU=1; shift ;;
    --screenshot) SHOT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --*) usage; exit 2 ;;
    *) CANDIDATE="$1"; shift ;;
  esac
done

run_cpu_math() {
  local cand="$1"
  local src="$cand/src/shadow_math.cpp"
  if [ ! -f "$src" ]; then
    printf 'FAIL: 候选缺少 src/shadow_math.cpp
' >&2
    return 2
  fi
  local cxx="${CXX:-}"
  if [ -z "$cxx" ]; then
    if command -v clang++ >/dev/null 2>&1; then cxx=clang++
    elif command -v g++ >/dev/null 2>&1; then cxx=g++
    else printf 'FAIL: 宿主没有 C++ 编译器
' >&2; return 2; fi
  fi
  local out
  out=$(mktemp -t vulkan001_math)
  if ! "$cxx" -std=c++17 -O2 -Wall -Wextra -I "$cand/src"       "$HERE/tests/test_shadow_math.cpp" "$src" -o "$out"; then
    printf 'FAIL: shadow_math.cpp 编译失败
' >&2
    return 1
  fi
  "$out" || { printf 'FAIL: shadow_math 数值测试未通过
' >&2; rm -f "$out"; return 1; }
  rm -f "$out"
  return 0
}

run_metrics_selftest() {
  python3 "$HERE/tests/test_image_metrics.py" >/dev/null 2>&1
}

compare_screenshot() {
  local cand="$1"
  local ref="$HERE/refs/scene_reference.ppm"
  local shot="$SHOT"
  if [ -z "$shot" ]; then
    shot="$cand/out/scene.ppm"
  fi
  if [ ! -f "$ref" ]; then
    printf 'SKIP: 参考图尚未生成（$ref），截图比对在 GPU 宿主机上做（见 hidden-tests/refs/README.md）\n'
    return 0
  fi
  if [ ! -f "$shot" ]; then
    fail "候选没有输出截图：$shot" 1
  fi
  python3 "$HERE/image_metrics.py" "$ref" "$shot" --mad 12 --ratio 0.92 || fail "截图相似度不达标" 1
}

run_gpu() {
  # TODO(run-on-host): 本段需要 Vulkan SDK（glslc）与可用 GPU/驱动，在 GPU 节点执行并归档日志。
  local cand="$1"
  command -v cmake >/dev/null 2>&1 || fail "缺少 cmake" 2
  command -v glslc >/dev/null 2>&1 || fail "缺少 glslc（Vulkan SDK）" 2
  ( cd "$cand" && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release ) || fail "cmake 配置失败" 1
  ( cd "$cand" && cmake --build build -j ) || fail "构建失败" 1
  ( cd "$cand" && mkdir -p out && ./build/vulkan_shadow --frames 120 --screenshot out/scene.ppm ) \
    || fail "运行失败（验证层可能有 error）" 1
  compare_screenshot "$cand"
}

if [ "$GPU" -eq 1 ]; then
  run_gpu "$CANDIDATE"
  exit 0
fi

if [ "$SELFTEST" -eq 0 ]; then
  python3 "$HERE/static_checks.py" "$CANDIDATE"
  SC=$?
  if [ "$SC" -eq 3 ]; then fail "契约文件被修改（硬门槛 HG3）" 3; fi
  if [ "$SC" -ne 0 ]; then fail "静态检查未通过" 1; fi
  run_cpu_math "$CANDIDATE"
  run_metrics_selftest
  compare_screenshot "$CANDIDATE"
  printf 'PASS: vulkan-001 隐藏验收通过（静态 + CPU 数学 + 截图工具）\n'
  exit 0
fi

TMP=$(mktemp -d -t vulkan001_self)
trap 'rm -rf "$TMP"' EXIT
PASS=0; FAILED=0

expect_ref_pass() {
  local label="$1" tree="$2"
  local ok=1
  python3 "$HERE/static_checks.py" "$tree" >/dev/null 2>&1 || ok=0
  run_cpu_math "$tree" >/dev/null 2>&1 || ok=0
  if [ "$ok" -eq 1 ]; then
    printf '  [OK]   %s：静态+CPU 数学通过（预期通过）\n' "$label"; PASS=$((PASS+1))
  else
    printf '  [FAIL] %s：预期通过但失败\n' "$label"
    python3 "$HERE/static_checks.py" "$tree" 2>&1 | tail -n 6
    run_cpu_math "$tree" 2>&1 | tail -n 6
    FAILED=$((FAILED+1))
  fi
}

printf 'vulkan-001 selftest\n'
for REF in a b; do
  TREE="$TMP/ref-$REF"
  cp -R "$TASK_ROOT/starter" "$TREE"
  ( cd "$TREE" && patch -p1 -s < "$TASK_ROOT/answers/reference-$REF.patch" ) || fail "参考解 $REF patch 应用失败" 2
  expect_ref_pass "reference-$REF" "$TREE"
done

TREE="$TMP/starter"
cp -R "$TASK_ROOT/starter" "$TREE"
if python3 "$HERE/static_checks.py" "$TREE" >/dev/null 2>&1; then
  printf '  [FAIL] starter：静态检查意外通过\n'; FAILED=$((FAILED+1))
else
  printf '  [OK]   starter：静态检查失败（预期失败）\n'; PASS=$((PASS+1))
fi
if run_cpu_math "$TREE" >/dev/null 2>&1; then
  printf '  [FAIL] starter：CPU 数学测试意外通过\n'; FAILED=$((FAILED+1))
else
  printf '  [OK]   starter：CPU 数学测试失败（预期失败）\n'; PASS=$((PASS+1))
fi

TAMPERED="$TMP/tampered"
cp -R "$TASK_ROOT/starter" "$TAMPERED"
printf '\n// tampered\n' >> "$TAMPERED/src/shadow_math.h"
python3 "$HERE/static_checks.py" "$TAMPERED" >/dev/null 2>&1
if [ "$?" -eq 3 ]; then
  printf '  [OK]   契约文件被修改：哈希校验拦截（预期拦截）\n'; PASS=$((PASS+1))
else
  printf '  [FAIL] 契约文件被修改：未拦截\n'; FAILED=$((FAILED+1))
fi

printf 'selftest: %s 通过 / %s 失败\n' "$PASS" "$FAILED"
[ "$FAILED" -eq 0 ] && { printf 'SELFTEST PASS\n'; exit 0; }
printf 'SELFTEST FAIL\n'; exit 1
