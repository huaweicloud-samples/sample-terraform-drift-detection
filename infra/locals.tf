# =============================================================================
# 全局定位信息与固定键名
# =============================================================================

data "huaweicloud_account" "current" {}

locals {
  domain_id      = data.huaweicloud_account.current.id
  project_id     = data.huaweicloud_account.current.current_project_id
  reports_bucket = "${var.name_prefix}-reports-${local.domain_id}"
  agency_name    = "${var.name_prefix}-cff-agency"

  obs_internal_endpoint = "obs.${var.region}.myhuaweicloud.com"

  # 构建机架构 → 离线工具包命名段。巡检与修复构建都从报告桶的 tools/ 前缀取包，
  # 包名的架构段必须与这里一致，否则换了构建机架构就会去取错架构的工具。
  # 注意：var.build_machine_arch 的取值是 CodeArts 接口要求的 x86-64 / arm，
  # 与离线包的 linux-amd64 / linux-arm64 命名段是两套取值空间，故作显式映射。
  tool_arch_map = {
    "x86-64" = "linux-amd64"
    arm      = "linux-arm64"
  }
  build_tool_arch = local.tool_arch_map[var.build_machine_arch]

  # 桶内固定键名：巡检结果前缀、受管资源源码包、受管资源状态文件、函数代码包
  plan_object_prefix   = "plans"
  source_object_key    = "source/target-infra.zip"
  state_object_key     = "state/target-infra.tfstate"
  orchestrator_zip_key = "functions/orchestrator.zip"
  classifier_zip_key   = "functions/classifier.zip"

  # 函数代码包在对象存储中的下载地址
  orchestrator_code_url = "https://${local.reports_bucket}.${local.obs_internal_endpoint}/${local.orchestrator_zip_key}"
  classifier_code_url   = "https://${local.reports_bucket}.${local.obs_internal_endpoint}/${local.classifier_zip_key}"

  # 编排函数注入的运行期配置，分类函数复用其中的存储与监控参数
  orchestrator_user_data = {
    PLAN_JOB_ID         = huaweicloud_codearts_build_task.plan.id
    APPLY_JOB_ID        = huaweicloud_codearts_build_task.apply.id
    CLASSIFIER_URN      = huaweicloud_fgs_function.classifier.urn
    REPORTS_BUCKET      = local.reports_bucket
    PLAN_KEY_PREFIX     = local.plan_object_prefix
    OBS_ENDPOINT        = "https://${local.obs_internal_endpoint}"
    SMN_TOPIC_URN       = huaweicloud_smn_topic.drift_alerts.topic_urn
    CES_NAMESPACE       = var.ces_metric_namespace
    CES_DIMENSION       = var.metric_dimension_name
    CES_DIMENSION_VALUE = var.metric_dimension_value
    HW_REGION           = var.region
    WAIT_SECONDS        = tostring(var.build_wait_seconds)
    POLL_INTERVAL       = tostring(var.build_poll_interval_seconds)
    APPROVE_COMMAND     = "terraform apply -auto-approve"
  }

  # 分级函数独立运行时也要能拼出区域项目级路径：
  # 上报自定义指标的接口路径以项目 ID 为前缀，主账号 ID 不能替代
  classifier_user_data = {
    REPORTS_BUCKET      = local.reports_bucket
    PLAN_KEY_PREFIX     = local.plan_object_prefix
    OBS_ENDPOINT        = "https://${local.obs_internal_endpoint}"
    CES_NAMESPACE       = var.ces_metric_namespace
    CES_DIMENSION       = var.metric_dimension_name
    CES_DIMENSION_VALUE = var.metric_dimension_value
    HW_REGION           = var.region
    HW_PROJECT_ID       = local.project_id
  }
}
