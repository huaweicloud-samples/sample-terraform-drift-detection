# =============================================================================
# 巡检编排与风险分级函数
#
# 两个函数的分工：
#   orchestrator  按顺序发起「巡检构建」，轮询构建状态直到结束，读取巡检结果，
#                 调用分类函数，再按分类结论触发「修复构建」或发出人工确认通知。
#   classifier    对巡检结果里的资源变更逐条判定风险等级并上报自定义指标。
#
# 代码包走对象存储地址挂载（code_type = "obs"）。
# =============================================================================

resource "huaweicloud_fgs_function" "orchestrator" {
  name        = "${var.name_prefix}-orchestrator"
  runtime     = var.function_runtime
  handler     = "index.main_handler"
  memory_size = var.orchestrator_memory_size
  # 编排函数需要覆盖「一次构建的最长等待 + 收尾调用」，故与等待参数联动
  timeout     = var.build_wait_seconds + 120
  description = "串接巡检构建、风险分级与修复动作"
  code_type   = "obs"
  code_url    = local.orchestrator_code_url
  agency      = huaweicloud_identity_agency.pipeline.name
  user_data   = jsonencode(local.orchestrator_user_data)
  # FunctionGraph 要求函数必须归属一个应用（app），接口路径里的 packageName
  # 不能为空。不声明时 provider 会传空值，创建报 FSS.1027 Invalid app name。
  app         = "default"

  enable_lts_log   = true
  log_group_id     = huaweicloud_lts_group.pipeline.id
  log_group_name   = huaweicloud_lts_group.pipeline.group_name
  log_stream_id    = huaweicloud_lts_stream.orchestrator.id
  log_stream_name  = huaweicloud_lts_stream.orchestrator.stream_name

  tags = merge(var.tags, {
    FunctionRole = "orchestrator"
  })

  lifecycle {
    # 代码包重新上传后函数需重建，否则仍运行旧包
    replace_triggered_by = [huaweicloud_obs_bucket_object.orchestrator_code]
  }
}

resource "huaweicloud_fgs_function" "classifier" {
  name        = "${var.name_prefix}-classifier"
  runtime     = var.function_runtime
  handler     = "index.main_handler"
  memory_size = var.classifier_memory_size
  timeout     = var.classifier_timeout
  description = "按资源类型判定变更风险等级"
  code_type   = "obs"
  code_url    = local.classifier_code_url
  agency      = huaweicloud_identity_agency.pipeline.name
  user_data   = jsonencode(local.classifier_user_data)
  # 同 orchestrator：函数必须归属一个应用，否则报 FSS.1027。
  app         = "default"

  enable_lts_log   = true
  log_group_id     = huaweicloud_lts_group.pipeline.id
  log_group_name   = huaweicloud_lts_group.pipeline.group_name
  log_stream_id    = huaweicloud_lts_stream.classifier.id
  log_stream_name  = huaweicloud_lts_stream.classifier.stream_name

  tags = merge(var.tags, {
    FunctionRole = "classifier"
  })

  lifecycle {
    replace_triggered_by = [huaweicloud_obs_bucket_object.classifier_code]
  }
}

# -----------------------------------------------------------------------------
# 定时巡检：每 6 小时一次，Cron 为 6 段（秒 分 时 日 月 周）
# -----------------------------------------------------------------------------

resource "huaweicloud_fgs_trigger" "scheduled_scan" {
  function_urn = huaweicloud_fgs_function.orchestrator.urn
  type         = "TIMER"
  # 触发器状态只有 ACTIVE / DISABLED 两个取值
  status = var.trigger_enabled ? "ACTIVE" : "DISABLED"

  timer {
    name          = "${var.name_prefix}-every-6-hours"
    schedule      = var.schedule_cron
    schedule_type = "Cron"
  }

  lifecycle {
    # timer 全部字段为强制新建项，且生效中的定时触发器不支持原地改写；
    # 调整巡检周期时请先销毁该触发器再重建。
    # status 由运维在函数控制台按巡检窗口临时启停，同样让出所有权。
    ignore_changes = [timer, status]
  }
}

# -----------------------------------------------------------------------------
# 分级函数执行失败时投递到告警主题：
# 同步调用由编排函数自行捕获异常，异步调用失败由该配置兜底通知。
# 目标类型取值为大写的 OBS / SMN / DIS / FunctionGraph，
# param 是这些服务所需参数的 JSON 字符串（SMN 为 topic_urn）。
# -----------------------------------------------------------------------------

resource "huaweicloud_fgs_async_invoke_configuration" "classifier_failure" {
  function_urn                   = huaweicloud_fgs_function.classifier.urn
  max_async_retry_attempts       = 2
  max_async_event_age_in_seconds = 3600
  enable_async_status_log        = true

  on_failure {
    destination = "SMN"
    param       = jsonencode({ topic_urn = huaweicloud_smn_topic.drift_alerts.topic_urn })
  }
}

# -----------------------------------------------------------------------------
# 调用链追踪：追踪接入密钥由 APM 侧签发，函数只引用其输出
# -----------------------------------------------------------------------------

resource "huaweicloud_apm_aksk" "pipeline" {
  count       = var.enable_tracing ? 1 : 0
  description = "漂移巡检函数调用链追踪接入密钥"
}

resource "huaweicloud_fgs_function_tracing_configuration" "orchestrator" {
  count        = var.enable_tracing ? 1 : 0
  function_urn = huaweicloud_fgs_function.orchestrator.urn
  tracing_ak   = huaweicloud_apm_aksk.pipeline[0].access_key
  tracing_sk   = huaweicloud_apm_aksk.pipeline[0].secret_key
}

resource "huaweicloud_fgs_function_tracing_configuration" "classifier" {
  count        = var.enable_tracing ? 1 : 0
  function_urn = huaweicloud_fgs_function.classifier.urn
  tracing_ak   = huaweicloud_apm_aksk.pipeline[0].access_key
  tracing_sk   = huaweicloud_apm_aksk.pipeline[0].secret_key
}
