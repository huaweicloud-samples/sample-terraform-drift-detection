#!/usr/bin/env bash
# =============================================================================
# 修复构建脚本
#
# 把受管资源拉回代码声明的期望态，并把最新状态文件写回巡检报告桶。
# 只有被判定为低风险的变更才会走到这里，因此脚本本身不再做二次确认。
#
# 需要的构建任务变量与巡检脚本完全一致。
# =============================================================================

set -euo pipefail

WORKSPACE="${CODE_BUILD_WORKSPACE:-$(pwd)}"
TARGET_DIR="${WORKSPACE}/target-infra"
TOOLS_DIR="${WORKSPACE}/.tools"
OBS_BASE="obs://${REPORTS_BUCKET}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# -----------------------------------------------------------------------------
# 离线工具链与代码、状态文件：与巡检脚本一致
# -----------------------------------------------------------------------------

prepare_tools() {
  mkdir -p "${TOOLS_DIR}/bin" "${TOOLS_DIR}/provider-mirror"

  obsutil cp "${OBS_BASE}/tools/terraform-${TERRAFORM_VERSION}-linux-amd64.tar.gz" \
    "${TOOLS_DIR}/terraform.tgz" -f >/dev/null
  tar -xzf "${TOOLS_DIR}/terraform.tgz" -C "${TOOLS_DIR}/bin"
  export PATH="${TOOLS_DIR}/bin:${PATH}"

  obsutil cp "${OBS_BASE}/tools/provider-mirror/" \
    "${TOOLS_DIR}/provider-mirror/" -r -f >/dev/null

  cat >"${TOOLS_DIR}/terraform.rc" <<EOF
provider_installation {
  filesystem_mirror {
    path    = "${TOOLS_DIR}/provider-mirror"
    include = ["registry.terraform.io/*/*"]
  }
}
EOF
  export TF_CLI_CONFIG_FILE="${TOOLS_DIR}/terraform.rc"
}

prepare_source() {
  if [[ ! -f "${TARGET_DIR}/main.tf" ]]; then
    obsutil cp "${OBS_BASE}/${SOURCE_KEY}" "${WORKSPACE}/source.zip" -f >/dev/null
    unzip -q -o "${WORKSPACE}/source.zip" -d "${WORKSPACE}"
  fi

  if obsutil stat "${OBS_BASE}/${STATE_KEY}" >/dev/null 2>&1; then
    log "取回上次的状态文件"
    obsutil cp "${OBS_BASE}/${STATE_KEY}" "${TARGET_DIR}/terraform.tfstate" -f >/dev/null
  fi
}

# -----------------------------------------------------------------------------
# 修复并回写状态
# -----------------------------------------------------------------------------

run_apply() {
  terraform -chdir="${TARGET_DIR}" init -input=false >/dev/null
  terraform -chdir="${TARGET_DIR}" apply -input=false -auto-approve

  obsutil cp "${TARGET_DIR}/terraform.tfstate" "${OBS_BASE}/${STATE_KEY}" -f >/dev/null
  log "最新状态文件已写回 ${STATE_KEY}"

  # 修复完成后把本次执行摘要也留档，便于事后追溯
  terraform -chdir="${TARGET_DIR}" show -json \
    >"${WORKSPACE}/apply_result.json"
  obsutil cp "${WORKSPACE}/apply_result.json" \
    "${OBS_BASE}/applies/$(date -u +%Y%m%dT%H%M%SZ)/apply_result.json" -f >/dev/null
}

prepare_tools
prepare_source
run_apply
