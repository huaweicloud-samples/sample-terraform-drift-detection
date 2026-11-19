# =============================================================================
# 函数运行日志
#
# 两个函数各自写入一个日志流，保留期由 log_retention_days 控制。
# 保留期在运行期可由日志接入端按需调整，因此三处都让出该字段的所有权，
# 避免控制台上的调整被下一次巡检计划改回去。
# =============================================================================

resource "huaweicloud_lts_group" "pipeline" {
  group_name  = "${var.name_prefix}-logs"
  ttl_in_days = var.log_retention_days
  tags = merge(var.tags, {
    Purpose = "drift-pipeline-runtime-logs"
  })

  lifecycle {
    ignore_changes = [ttl_in_days]
  }
}

resource "huaweicloud_lts_stream" "orchestrator" {
  group_id    = huaweicloud_lts_group.pipeline.id
  stream_name = "${var.name_prefix}-orchestrator"
  ttl_in_days = var.log_retention_days

  lifecycle {
    ignore_changes = [ttl_in_days]
  }
}

resource "huaweicloud_lts_stream" "classifier" {
  group_id    = huaweicloud_lts_group.pipeline.id
  stream_name = "${var.name_prefix}-classifier"
  ttl_in_days = var.log_retention_days

  lifecycle {
    ignore_changes = [ttl_in_days]
  }
}
