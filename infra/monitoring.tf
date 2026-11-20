# =============================================================================
# 指标监控：巡检大盘
#
# 指标由分类函数在运行时上报到自定义命名空间（var.ces_metric_namespace），
# 本文件只声明「读哪些指标、如何展示」。
# 自定义命名空间、指标名与维度名必须和函数代码中的取值完全一致，否则大盘无数据。
# =============================================================================

locals {
  # 函数上报的指标名清单：大盘组件按此清单取数，与函数代码一一对应
  drift_metric_names = {
    detected  = "DriftDetected"
    no_drift  = "NoDrift"
    critical  = "Drift_Critical"
    high      = "Drift_High"
    low       = "Drift_Low"
    res_count = "DriftResourceCount"
  }

  # 巡检大盘组件：每行三个，共三列
  dashboard_widgets = [
    {
      key     = "drift-result"
      title   = "巡检结论"
      metrics = [local.drift_metric_names.detected, local.drift_metric_names.no_drift]
      left    = 0
      top     = 0
    },
    {
      key   = "drift-severity"
      title = "变更风险分级"
      metrics = [
        local.drift_metric_names.critical,
        local.drift_metric_names.high,
        local.drift_metric_names.low,
      ]
      left = 4
      top  = 0
    },
    {
      key     = "drift-resource-count"
      title   = "漂移资源数"
      metrics = [local.drift_metric_names.res_count]
      left    = 8
      top     = 0
    },
  ]
}

# -----------------------------------------------------------------------------
# 关于指标告警规则
#
# 本模块不创建 `huaweicloud_ces_alarmrule`：该资源在 1.96.1 版本上对非 ECS
# 命名空间存在指标白名单校验缺陷（自定义命名空间的指标会被映射到其它服务的
# 默认指标，创建时报 ces.0014 check metric whitelist failed）。
# 需要阈值告警时，改用 CES SDK 直连接口上报并建规则，或在云监控控制台手动创建；
# 通知链路本身不依赖告警规则——严重变更由函数直接发布到消息通知主题。
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# 巡检大盘
# -----------------------------------------------------------------------------

resource "huaweicloud_ces_dashboard" "drift" {
  count          = var.enable_dashboard ? 1 : 0
  name           = "${var.name_prefix}-dashboard"
  row_widget_num = 3

  extend_info {
    enable_legend = true
    period        = "300"
    refresh_time  = 60
  }
}

resource "huaweicloud_ces_dashboard_widget" "drift" {
  for_each = var.enable_dashboard ? { for w in local.dashboard_widgets : w.key => w } : {}

  dashboard_id        = huaweicloud_ces_dashboard.drift[0].id
  title               = each.value.title
  view                = var.dashboard_widget_view
  metric_display_mode = var.dashboard_metric_display_mode
  unit                = "Count"

  location {
    left   = each.value.left
    top    = each.value.top
    width  = 4
    height = 3
  }

  dynamic "metrics" {
    for_each = each.value.metrics
    content {
      namespace   = var.ces_metric_namespace
      metric_name = metrics.value

      dimensions {
        name        = var.metric_dimension_name
        filter_type = var.dashboard_dimension_filter_type
        values      = [var.metric_dimension_value]
      }
    }
  }

  properties {
    top_n = 5
    order = "desc"
  }
}
