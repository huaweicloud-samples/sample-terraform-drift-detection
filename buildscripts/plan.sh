#!/usr/bin/env bash
# =============================================================================
# 巡检构建脚本
#
# 在构建机内完成：备齐离线工具链 → 取回状态文件 → terraform plan
# → 把结构化巡检结果写回巡检报告桶。
#
# 构建任务需注入的变量（由巡检环境创建时下发）：
#   REPORTS_BUCKET     巡检报告桶名
#   OBS_ENDPOINT       对象存储内网访问地址，形如 obs.cn-north-4.myhuaweicloud.com
#   SOURCE_KEY         受管资源代码包在桶内的对象名
#   STATE_KEY          受管资源状态文件在桶内的对象名
#   TERRAFORM_VERSION  离线工具包对应的 Terraform 版本
#   HW_REGION          区域
# 凭据由构建任务的服务授权提供，脚本内不出现任何密钥。
# =============================================================================

set -euo pipefail

WORKSPACE="${CODE_BUILD_WORKSPACE:-$(pwd)}"
TARGET_DIR="${WORKSPACE}/target-infra"
TOOLS_DIR="${WORKSPACE}/.tools"
OBS_BASE="obs://${REPORTS_BUCKET}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PLAN_PREFIX="${PLAN_KEY_PREFIX:-plans}"
# 离线工具包的架构段由构建环境给出，与构建机架构保持一致
TOOL_ARCH="${TOOL_ARCH:-linux-amd64}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# -----------------------------------------------------------------------------
# 1. 离线工具链
#
# Terraform 可执行文件与各 provider 插件提前上传到巡检报告桶的 tools/ 前缀，
# 构建机只在内网访问对象存储，不从公网下载地址取包。
#   tools/terraform-<版本>-<架构段>.tar.gz（x86_64 构建机为 linux-amd64，arm64 为 linux-arm64）
#   tools/provider-mirror/registry.terraform.io/huaweicloud/huaweicloud/<版本>/linux_amd64/…
# 上传方式：用 obsutil 把工具包与插件镜像目录整体上传到巡检报告桶的 tools/ 前缀，
# 保持上面的层级不变即可。
# -----------------------------------------------------------------------------

prepare_tools() {
  mkdir -p "${TOOLS_DIR}/bin" "${TOOLS_DIR}/provider-mirror"

  obsutil cp "${OBS_BASE}/tools/terraform-${TERRAFORM_VERSION}-linux-amd64.tar.gz" \
    "${TOOLS_DIR}/terraform.tgz" -f >/dev/null
  tar -xzf "${TOOLS_DIR}/terraform.tgz" -C "${TOOLS_DIR}/bin"
  export PATH="${TOOLS_DIR}/bin:${PATH}"

  # 插件镜像目录整体取回；obsutil 递归下载
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

# -----------------------------------------------------------------------------
# 2. 受管资源代码与状态文件
# -----------------------------------------------------------------------------

prepare_source() {
  if [[ ! -f "${TARGET_DIR}/main.tf" ]]; then
    log "工作区内没有受管资源代码，从桶内取回代码包"
    obsutil cp "${OBS_BASE}/${SOURCE_KEY}" "${WORKSPACE}/source.zip" -f >/dev/null
    unzip -q -o "${WORKSPACE}/source.zip" -d "${WORKSPACE}"
  fi

  if obsutil stat "${OBS_BASE}/${STATE_KEY}" >/dev/null 2>&1; then
    log "取回上次的状态文件"
    obsutil cp "${OBS_BASE}/${STATE_KEY}" "${TARGET_DIR}/terraform.tfstate" -f >/dev/null
  else
    log "桶内暂无状态文件，本次按首次巡检处理"
  fi
}

# -----------------------------------------------------------------------------
# 3. 巡检
#
# -detailed-exitcode：0 = 无变更，1 = 出错，2 = 存在差异
# -----------------------------------------------------------------------------

run_plan() {
  terraform -chdir="${TARGET_DIR}" init -input=false >/dev/null
  terraform -chdir="${TARGET_DIR}" validate

  local rc=0
  terraform -chdir="${TARGET_DIR}" plan -input=false \
    -out=plan.tfplan -detailed-exitcode || rc=$?

  if [[ "${rc}" == "1" ]]; then
    log "巡检执行失败"
    exit 1
  fi

  if [[ "${rc}" == "2" ]]; then
    terraform -chdir="${TARGET_DIR}" show -json plan.tfplan \
      >"${WORKSPACE}/drift_result.json"
    log "检测到配置漂移，详见 drift_result.json"
  else
    printf '{"drift_detected": false, "resource_changes": []}\n' \
      >"${WORKSPACE}/drift_result.json"
    log "未发现配置漂移"
  fi

  obsutil cp "${WORKSPACE}/drift_result.json" \
    "${OBS_BASE}/${PLAN_PREFIX}/${TIMESTAMP}/drift_result.json" -f >/dev/null
  log "巡检结果已写入 ${PLAN_PREFIX}/${TIMESTAMP}/drift_result.json"

  # 供后续修复构建复用同一份计划
  obsutil cp "${TARGET_DIR}/plan.tfplan" \
    "${OBS_BASE}/${PLAN_PREFIX}/${TIMESTAMP}/plan.tfplan" -f >/dev/null || true
}

prepare_tools
prepare_source
run_plan
