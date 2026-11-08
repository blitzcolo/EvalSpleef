#!/usr/bin/env bash
# video-001 隐藏验收入口。
#
# 用法：
#   bash hidden-tests/run.sh [候选工程目录]    # 完整验收：渲染 MP4 + ffprobe + 抽帧（需要 ffmpeg/ffprobe）
#   bash hidden-tests/run.sh --frames-only     # 仅逐帧检查（无需 ffmpeg，用于本地开发）
#   bash hidden-tests/run.sh --selftest        # 验证装置自检（无需 ffmpeg）
#
# 退出码：0=通过；1=不达标；2=环境缺少 ffmpeg（基础设施错误，按 docs/02 §8 重试/换机）；
#         3=测试文件被修改（硬门槛 HG2）
#
# TODO(run-on-host)：完整验收需要在装有 ffmpeg/ffprobe 的验证机上执行一次，本仓库开发机没有 ffmpeg。
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
TASK_ROOT=$(cd "$HERE/.." && pwd)
LOCK_MANIFEST="$HERE/locks.sha256"
SELFTEST=0
FRAMES_ONLY=0
CANDIDATE="$TASK_ROOT/starter"
TIMES="1.0 5.0 12.0 20.0 26.0 29.0"

usage() {
  printf '用法: bash hidden-tests/run.sh [候选目录 | --frames-only | --selftest]\n'
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

render_frames() {
  local candidate="$1"
  local outdir="$2"
  mkdir -p "$outdir"
  for moment in $TIMES; do
    ( cd "$candidate" && python3 intro.py --render-frame "$moment" --out "$outdir/t_$moment.ppm" ) >/dev/null 2>&1 \
      || return 1
  done
}

run_frames_checks() {
  local candidate="$1"
  local tmp="$2"
  python3 "$HERE/plan_check.py" "$candidate" || return 1
  render_frames "$candidate" "$tmp/frames" || { printf 'FAIL: 逐帧渲染失败\n'; return 1; }
  python3 "$HERE/frames.py" --dir "$tmp/frames"
}

render_video() {
  local candidate="$1"
  local outfile="$2"
  ( cd "$candidate" && python3 intro.py --out "$outfile" )
}

if [ "$#" -ge 1 ]; then
  case "$1" in
    --selftest) SELFTEST=1 ;;
    --frames-only) FRAMES_ONLY=1 ;;
    -h|--help) usage; exit 0 ;;
    --*) usage; exit 2 ;;
    *) CANDIDATE="$1" ;;
  esac
fi

if [ ! -d "$CANDIDATE" ]; then
  printf 'ERROR: 候选目录不存在: %s\n' "$CANDIDATE" >&2
  exit 2
fi

TMP=$(mktemp -d "${TMPDIR:-/tmp}/video001.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

if [ "$SELFTEST" -eq 0 ]; then
  check_locks "$CANDIDATE"
  python3 "$HERE/probe.py" --selftest >/dev/null || fail "probe.py 自检失败" 2
  python3 "$HERE/frames.py" --selftest >/dev/null || fail "frames.py 自检失败" 2
  if [ "$FRAMES_ONLY" -eq 1 ]; then
    run_frames_checks "$CANDIDATE" "$TMP" || fail "逐帧检查未通过"
    printf 'PASS: video-001 逐帧检查通过（未做 ffprobe 编码验收）\n'
    exit 0
  fi
  if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    printf 'INFRA: 缺少 ffmpeg/ffprobe，无法执行完整导出验收（环境错误）\n' >&2
    exit 2
  fi
  run_frames_checks "$CANDIDATE" "$TMP" || fail "逐帧检查未通过"
  MP4="$TMP/intro.mp4"
  render_video "$CANDIDATE" "$MP4" || fail "MP4 渲染失败"
  python3 "$HERE/probe.py" --input "$MP4" || fail "ffprobe 硬门槛未通过"
  mkdir -p "$TMP/extracted"
  for moment in $TIMES; do
    ffmpeg -v error -ss "$moment" -i "$MP4" -frames:v 1 -pix_fmt rgb24 -f image2 -y \
      "$TMP/extracted/t_$moment.ppm" || fail "抽帧失败 t=$moment"
  done
  python3 "$HERE/frames.py" --dir "$TMP/extracted" || fail "抽帧结构检查未通过"
  printf 'PASS: video-001 完整验收通过（ffprobe + 6 帧结构）\n'
  exit 0
fi

# ---------------------------------------------------------------- selftest
check_locks "$TASK_ROOT/starter" >/dev/null 2>&1 || fail "starter 应通过哈希锁校验" 2
PASS_COUNT=0
FAIL_COUNT=0

expect() {
  local label="$1"
  local expected="$2"
  local actual="$3"
  if [ "$expected" = "$actual" ]; then
    printf '  [OK]   %s（退出码 %s）\n' "$label" "$actual"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    printf '  [FAIL] %s：期望 %s 实际 %s\n' "$label" "$expected" "$actual"
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

printf 'video-001 selftest\n'
python3 "$HERE/probe.py" --selftest >/dev/null 2>&1
expect "probe.py 样例自检" 0 "$?"
python3 "$HERE/frames.py" --selftest >/dev/null 2>&1
expect "frames.py 合成用例自检" 0 "$?"

for REF in a b; do
  TREE="$TMP/ref-$REF"
  cp -R "$TASK_ROOT/starter" "$TREE"
  ( cd "$TREE" && patch -p1 -s < "$TASK_ROOT/answers/reference-$REF.patch" ) || fail "参考解 $REF patch 应用失败" 2
  python3 "$HERE/check_locks.py" "$LOCK_MANIFEST" "$TREE" >/dev/null 2>&1
  expect "reference-$REF 哈希锁" 0 "$?"
  run_frames_checks "$TREE" "$TMP/ref-$REF-tmp" >/dev/null 2>&1
  expect "reference-$REF 逐帧检查" 0 "$?"
done

run_frames_checks "$TASK_ROOT/starter" "$TMP/starter-tmp" >/dev/null 2>&1
expect "starter（1280x720/24s/1 段）逐帧检查" 1 "$?"

TAMPERED="$TMP/tampered"
cp -R "$TASK_ROOT/starter" "$TAMPERED"
printf '\n# agent tampered\n' >> "$TAMPERED/tests/test_intro.py"
python3 "$HERE/check_locks.py" "$LOCK_MANIFEST" "$TAMPERED" >/dev/null 2>&1
expect "tests/ 被修改的哈希锁" 3 "$?"

python3 - "$TMP/ref-a" <<'PY'
import subprocess, sys
candidate = sys.argv[1]
frames = []
for moment in ("5.0", "5.0"):
    out = f"/tmp/video001_det_{moment}.ppm"
    subprocess.run([sys.executable, "intro.py", "--render-frame", moment, "--out", out], cwd=candidate, check=True)
    frames.append(open(out, "rb").read())
sys.exit(0 if frames[0] == frames[1] else 1)
PY
expect "参考解 A 单帧确定性" 0 "$?"

printf 'selftest: %s 通过 / %s 失败\n' "$PASS_COUNT" "$FAIL_COUNT"
if [ "$FAIL_COUNT" -eq 0 ]; then
  printf 'SELFTEST PASS\n'
  exit 0
fi
printf 'SELFTEST FAIL\n'
exit 1
