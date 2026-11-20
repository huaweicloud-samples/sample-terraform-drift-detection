# =============================================================================
# 输出
# =============================================================================

output "orchestrator_function_urn" {
  description = "编排函数 URN"
  value       = huaweicloud_fgs_function.orchestrator.urn
}

output "classifier_function_urn" {
  description = "分类函数 URN"
  value       = huaweicloud_fgs_function.classifier.urn
}

output "reports_bucket" {
  description = "存放巡检报告、函数代码包与受管资源状态文件的桶名"
  value       = huaweicloud_obs_bucket.reports.bucket
}

output "plan_object_prefix" {
  description = "巡检结果在桶内的前缀"
  value       = local.plan_object_prefix
}

output "managed_state_object_key" {
  description = "受管资源状态文件在桶内的对象名"
  value       = local.state_object_key
}

output "source_object_key" {
  description = "受管资源代码包在桶内的对象名"
  value       = local.source_object_key
}

output "plan_build_job_id" {
  description = "巡检构建任务 ID，编排函数按此 ID 发起构建"
  value       = huaweicloud_codearts_build_task.plan.id
}

output "apply_build_job_id" {
  description = "修复构建任务 ID，低风险变更自动修复时按此 ID 发起构建"
  value       = huaweicloud_codearts_build_task.apply.id
}

output "alert_topic_urn" {
  description = "漂移告警主题 URN"
  value       = huaweicloud_smn_topic.drift_alerts.topic_urn
}

output "encryption_key_id" {
  description = "报告与代码包加密使用的主密钥 ID"
  value       = huaweicloud_kms_key.pipeline.id
}

output "agency_name" {
  description = "函数运行权限委托名"
  value       = huaweicloud_identity_agency.pipeline.name
}

output "log_group_name" {
  description = "函数运行日志所在的日志组"
  value       = huaweicloud_lts_group.pipeline.group_name
}

output "dashboard_id" {
  description = "巡检监控大盘 ID"
  value       = var.enable_dashboard ? huaweicloud_ces_dashboard.drift[0].id : ""
}

output "scan_cron" {
  description = "当前生效的巡检周期表达式"
  value       = var.schedule_cron
}
