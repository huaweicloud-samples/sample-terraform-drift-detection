# =============================================================================
# 受管资源：漂移巡检的目标
#
# 这里的资源刻意保持简单：一个对象存储桶、一条桶策略、一个安全组与两条规则、
# 一个用户组与一条自定义权限。它们覆盖「标签 / 加密 / 访问控制 / 权限」四类
# 最常见的配置漂移来源，便于观察巡检结论与分级结果。
#
# 状态文件在本目录内生成，构建脚本负责把它同步到巡检报告桶的固定对象：
# 巡检按「定时触发 → 单次构建 → 结束」串行执行，同一时刻只有一个任务持有状态，
# 因此不需要额外的状态加锁服务。取回与写回见仓库根目录 buildscripts/plan.sh
# 与 buildscripts/apply.sh。
# =============================================================================

terraform {
  required_version = ">= 1.5"

  # 状态为本地文件，由构建脚本在每次执行前后与桶内对象同步。
  # 巡检按「定时触发 → 单次构建 → 结束」串行执行，同一时刻只有一个构建任务
  # 持有状态，不存在并发写入，因此不引入状态加锁服务。
  backend "local" {}

  required_providers {
    huaweicloud = {
      source  = "huaweicloud/huaweicloud"
      version = "~> 1.96.1"
    }
  }
}

provider "huaweicloud" {
  region = var.region
  # 凭据由构建任务注入环境变量 HW_ACCESS_KEY / HW_SECRET_KEY
}

# -----------------------------------------------------------------------------
# 对象存储：应用静态资源
# -----------------------------------------------------------------------------

resource "huaweicloud_obs_bucket" "assets" {
  bucket        = var.assets_bucket
  storage_class = "STANDARD"
  force_destroy = true

  versioning    = true
  encryption    = true
  sse_algorithm = "kms"
  kms_key_id    = var.assets_kms_key_id

  tags = merge(var.tags, {
    Environment = "production"
    Owner       = "platform-team"
  })
}

# 只允许 HTTPS 访问
resource "huaweicloud_obs_bucket_policy" "assets_tls" {
  bucket        = huaweicloud_obs_bucket.assets.id
  policy_format = "obs"

  policy = jsonencode({
    Version = "2008-10-17"
    Statement = [
      {
        Sid       = "EnforceTLSOnly"
        Effect    = "Deny"
        Principal = { ID = ["*"] }
        Action    = ["*"]
        Resource = [
          huaweicloud_obs_bucket.assets.bucket,
          "${huaweicloud_obs_bucket.assets.bucket}/*",
        ]
        Condition = {
          Bool = {
            SecureTransport = ["false"]
          }
        }
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# 安全组：仅放行内网 443
# -----------------------------------------------------------------------------

resource "huaweicloud_networking_secgroup" "web" {
  name                 = "${var.name_prefix}-web"
  description          = "只放通内网 HTTPS 访问"
  delete_default_rules = true

  tags = var.tags
}

resource "huaweicloud_networking_secgroup_rule" "https_ingress" {
  security_group_id = huaweicloud_networking_secgroup.web.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 443
  port_range_max    = 443
  remote_ip_prefix  = var.allowed_cidr
  description       = "内网 HTTPS"
}

resource "huaweicloud_networking_secgroup_rule" "all_egress" {
  security_group_id = huaweicloud_networking_secgroup.web.id
  direction         = "egress"
  ethertype         = "IPv4"
  description       = "放通全部出站"
}

# -----------------------------------------------------------------------------
# 权限：用户组 + 自定义权限
# -----------------------------------------------------------------------------

resource "huaweicloud_identity_group" "app_deployers" {
  name        = "${var.name_prefix}-app-deployers"
  description = "应用发布用户组"
}

resource "huaweicloud_identity_role" "asset_reader" {
  name        = "${var.name_prefix}-asset-reader"
  description = "只读访问应用静态资源"
  type        = "AX"

  policy = jsonencode({
    Version = "1.1"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["obs:objects:getObject", "obs:bucket:listBucket"]
        Resource = ["*"]
      }
    ]
  })
}

resource "huaweicloud_identity_group_role_assignment" "asset_reader" {
  group_id = huaweicloud_identity_group.app_deployers.id
  role_id  = huaweicloud_identity_role.asset_reader.id
}
