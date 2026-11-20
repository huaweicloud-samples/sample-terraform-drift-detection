# =============================================================================
# 函数运行权限委托
#
# 函数以委托临时凭证访问对象存储、密钥管理、消息通知、云监控与构建服务，
# 代码中不出现任何长期密钥。
#
# IAM 角色分两类作用域，授错层级时委托创建成功但角色不进 token（调用才报 403）：
#   AX（全局服务级）→ domain_roles
#   XA（项目级）    → project_role.roles
# 变量默认值按本实践所需权限归类；换用其它角色前先在 IAM 权限列表核对角色 type。
# =============================================================================

resource "huaweicloud_identity_agency" "pipeline" {
  name                   = local.agency_name
  delegated_service_name = "op_svc_cff"
  description            = "漂移巡检流水线中函数访问云服务使用的委托"

  domain_roles = var.agency_domain_roles

  project_role {
    project = var.region
    roles   = var.agency_project_roles
  }
}
