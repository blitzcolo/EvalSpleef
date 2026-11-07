#!/usr/bin/env bash
# macos-001 XCTest 入口（需要在装有 Xcode 的 macOS 主机执行）。
#
# Tests/ParticleTests.swift 是 XCTest 用例；本仓库以 swiftc 直接构建可执行文件（无 .xcodeproj），
# 因此 XCTest 需要一个宿主工程/包。二选一：
#   1) 在本目录执行 swift package init 后把 Sources/ 与 Tests/ 纳入 SwiftPM 包；
#   2) 在 Xcode 里新建 macOS Command Line Tool target 后加入 ParticleTests.swift。
# TODO(run-on-host)：当前仓库未提交 .xcodeproj；Xcode 主机上完成 1)或 2) 后把命令写死在这里。
set -u
printf 'XCTest 需要 Xcode 宿主工程/包（见本脚本注释）。TODO(run-on-host)\n' >&2
printf '可先用 scripts/perf_host.sh 完成性能验收；功能检查用 hidden-tests/run.sh。\n' >&2
exit 2
