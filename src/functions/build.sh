#!/usr/bin/env bash
# =============================================================================
# 函数代码包构建入口
#
# 产物路径与 infra/variables.tf 的默认取值一致：
#   src/functions/build/orchestrator.zip   编排函数代码包
#   src/functions/build/classifier.zip     分级函数代码包
#   build/target-infra.zip                 受管资源源码包
#
# 构建过程只做本地校验：语法检查、入口位置检查、禁止内置依赖混入检查，
# 不访问任何云端接口。
#
# 用法：
#   bash src/functions/build.sh                      # 全部产物
#   bash src/functions/build.sh orchestrator         # 只构建一个函数包
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

if [[ $# -gt 0 ]]; then
  exec "${PYTHON_BIN}" "${REPO_ROOT}/scripts/package_functions.py" --only "$@"
fi

exec "${PYTHON_BIN}" "${REPO_ROOT}/scripts/package_functions.py"
