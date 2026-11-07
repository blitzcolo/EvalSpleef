#!/usr/bin/env bash
# macos-001 裸机性能机验收脚本：5 次独立运行取中位数 + 测前温度/负载前置检查。
# 必须在**裸机性能机**上以临时用户执行（VM 内 GPU 半虚拟化，性能不代表真机）。
# 用法：bash scripts/perf_host.sh [工程目录]
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
TARGET="$ROOT"
if [ "$#" -ge 1 ]; then
  TARGET="$1"
fi
CHECKS="$ROOT/hidden-tests/checks.py"

if [ "$(uname -s)" != "Darwin" ]; then
  printf 'INFRA: 需要 macOS 裸机性能机\n' >&2
  exit 2
fi

printf '== 测前环境检查 ==\n'
printf '机型: %s\n' "$(sysctl -n hw.model)"
printf '芯片: %s\n' "$(sysctl -n machdep.cpu.brand_string)"
printf '负载(1/5/15 分钟): %s\n' "$(sysctl -n vm.loadavg | tr -d '{}')"
printf '温度/功耗限制: %s\n' "$(pmset -g therm 2>/dev/null | tr '\n' ' ' | cut -c1-200)"
LOAD1=$(sysctl -n vm.loadavg | awk '{print $2}')
printf 'LOW_POWER_MODE=%s\n' "$(pmset -g 2>/dev/null | grep -i lowpowermode | awk '{print $2}')"

# TODO(run-on-host)：性能机要求 load1 < 2.0 且 LowPowerMode=0，否则本轮测量作废重排。
if [ "$(printf '%s' "$LOAD1" | cut -d. -f1)" -ge 2 ]; then
  printf 'INFRA: 负载过高（load1=%s），本轮测量作废\n' "$LOAD1" >&2
  exit 2
fi

printf '== 5 次独立运行（每次 120 帧 / 24000 粒子 / 1080p）==\n'
( cd "$TARGET" && make bench >/dev/null 2>&1 ) || { printf 'ERROR: 构建失败\n' >&2; exit 2; }
python3 "$CHECKS" --mode perf --binary "$TARGET/build/ParticleBench" --workdir "$TARGET"
CODE=$?
if [ "$CODE" -eq 0 ]; then
  printf 'PASS: 性能机验收通过（中位帧时间 <= 16.7ms）\n'
else
  printf 'FAIL: 性能机验收未通过\n'
fi
exit "$CODE"
