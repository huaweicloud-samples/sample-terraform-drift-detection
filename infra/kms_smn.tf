# =============================================================================
# 密钥与消息通知
# =============================================================================
# 不声明 pending_days：该参数在 provider 中表示「创建后立即进入待删除日程」。
# 填 "7" 会让密钥建出来就处于 state=4（待删除），provider 等待就绪时报
# unexpected state '4', wanted target '2, 5'，随后所有引用它的资源连锁失败。
# 需要删除密钥时，请单独调用计划删除接口排期。

resource "huaweicloud_kms_key" "pipeline" {
  key_alias         = "${var.name_prefix}-datakey-v3"
  key_description   = "巡检报告与函数代码包的加密主密钥"
  is_enabled        = true
  rotation_enabled  = true
  rotation_interval = 365
  tags              = var.tags
}

# key_alias 是必填项，密钥创建时云服务会同步生成 alias/<key_alias> 指向本密钥，
# 因此不再单独声明 huaweicloud_kms_alias——同一别名重复绑定会在创建时报别名已存在。
# 密钥本身不声明密钥策略资源：当前 provider 的密钥资源无策略入参，
# 跨服务使用授权由委托与桶策略承担。

# -----------------------------------------------------------------------------
# 漂移告警主题与邮件订阅
#
# 消息通知服务的主题不提供自定义主密钥参数，主题级加密由服务侧托管；
# 本模块用同一把主密钥加密主题投递的巡检报告对象，保证链路端到端可控。
# -----------------------------------------------------------------------------

resource "huaweicloud_smn_topic" "drift_alerts" {
  name         = "${var.name_prefix}-alerts"
  display_name = "基础设施漂移告警"
  introduction = "漂移巡检的严重告警与审批提醒"
  tags         = var.tags
}

resource "huaweicloud_smn_subscription" "email" {
  topic_urn = huaweicloud_smn_topic.drift_alerts.topic_urn
  protocol  = "email"
  endpoint  = var.notification_email
  remark    = "漂移告警接收人"
}
