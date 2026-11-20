# =============================================================================
# 巡检报告与审计留痕存储
# =============================================================================

resource "huaweicloud_obs_bucket" "reports" {
  bucket             = local.reports_bucket
  storage_class      = "STANDARD"
  force_destroy      = true
  acl                = "private"
  versioning         = true
  encryption         = true
  sse_algorithm      = "kms"
  kms_key_id         = huaweicloud_kms_key.pipeline.id
  policy_format      = "obs"
  bucket_key_enabled = "true"

  tags = merge(var.tags, {
    Purpose = "drift-audit-trail"
  })

  lifecycle_rule {
    name    = "expire-old-reports"
    prefix  = "${local.plan_object_prefix}/"
    enabled = true

    expiration {
      days = var.report_retention_days
    }
  }

  lifecycle_rule {
    name    = "expire-noncurrent-versions"
    prefix  = ""
    enabled = true

    noncurrent_version_expiration {
      days = var.report_retention_days
    }
  }
}

# -----------------------------------------------------------------------------
# 关于对象访问日志
#
# 本模块不配置 `logging`：OBS 日志投递要求目标桶授权为 log-delivery-write 且
# 填写一个信任 OBS 云服务的委托名（logging.agency 为必填项），与下面的
# 公开访问阻断（block_public_acls / ignore_public_acls）以及桶级默认加密相冲突，
# 自投自桶的写法在运行期不会产出日志。函数运行日志已由 LTS 覆盖；
# 需要对象访问日志时，请另建独立日志桶与 OBS 委托后再接入。
# -----------------------------------------------------------------------------

# 桶级公开访问阻断：四项全开，禁止任何匿名读取
resource "huaweicloud_obs_bucket_bpa" "reports" {
  bucket                  = huaweicloud_obs_bucket.reports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# 仅允许 HTTPS 访问；OBS 桶策略的 Version / Principal / 条件键写法与
# IAM 自定义策略不同，条件值必须是字符串数组。
resource "huaweicloud_obs_bucket_policy" "reports_tls" {
  bucket        = huaweicloud_obs_bucket.reports.id
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
          huaweicloud_obs_bucket.reports.bucket,
          "${huaweicloud_obs_bucket.reports.bucket}/*",
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

# 委托临时凭证可读写报告桶：对象存储数据面只认桶策略，不认委托角色。
resource "huaweicloud_obs_bucket_policy" "reports_agency" {
  bucket        = huaweicloud_obs_bucket.reports.id
  policy_format = "obs"
  depends_on    = [huaweicloud_identity_agency.pipeline]

  policy = jsonencode({
    Version = "2008-10-17"
    Statement = [
      {
        Sid    = "AllowPipelineAgency"
        Effect = "Allow"
        Principal = {
          ID = ["domain/${local.domain_id}:agency/${local.agency_name}"]
        }
        Action = [
          "GetObject",
          "PutObject",
          "DeleteObject",
          "ListBucket",
        ]
        Resource = [
          huaweicloud_obs_bucket.reports.bucket,
          "${huaweicloud_obs_bucket.reports.bucket}/*",
        ]
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# 函数代码包与受管资源源码包
# -----------------------------------------------------------------------------

resource "huaweicloud_obs_bucket_object" "orchestrator_code" {
  bucket       = huaweicloud_obs_bucket.reports.id
  key          = local.orchestrator_zip_key
  source       = var.orchestrator_package
  content_type = "application/zip"
  encryption   = true
  kms_key_id   = huaweicloud_kms_key.pipeline.id
}

resource "huaweicloud_obs_bucket_object" "classifier_code" {
  bucket       = huaweicloud_obs_bucket.reports.id
  key          = local.classifier_zip_key
  source       = var.classifier_package
  content_type = "application/zip"
  encryption   = true
  kms_key_id   = huaweicloud_kms_key.pipeline.id
}

resource "huaweicloud_obs_bucket_object" "target_infra_source" {
  bucket       = huaweicloud_obs_bucket.reports.id
  key          = local.source_object_key
  source       = var.target_infra_package
  content_type = "application/zip"
  encryption   = true
  kms_key_id   = huaweicloud_kms_key.pipeline.id
}
