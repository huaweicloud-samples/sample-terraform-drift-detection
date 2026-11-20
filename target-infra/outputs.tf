# =============================================================================
# 输出
# =============================================================================

output "assets_bucket" {
  description = "应用静态资源的桶名"
  value       = huaweicloud_obs_bucket.assets.bucket
}

output "security_group_id" {
  description = "安全组 ID"
  value       = huaweicloud_networking_secgroup.web.id
}

output "deployer_group_id" {
  description = "应用发布用户组 ID"
  value       = huaweicloud_identity_group.app_deployers.id
}
