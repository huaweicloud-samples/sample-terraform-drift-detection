# =============================================================================
# 输入变量
# =============================================================================

variable "region" {
  description = "华为云区域"
  type        = string
  default     = "cn-north-4"
}

variable "name_prefix" {
  description = "资源名称前缀"
  type        = string
  default     = "tf-drift-target"
}

variable "assets_bucket" {
  description = "应用静态资源的桶名，须全局唯一"
  type        = string
}

variable "assets_kms_key_id" {
  description = "加密应用静态资源的主密钥 ID，由巡检环境创建后传入"
  type        = string
}

variable "allowed_cidr" {
  description = "允许访问 443 的内网网段"
  type        = string
  default     = "192.168.0.0/16"
}

variable "tags" {
  description = "为资源追加的标签"
  type        = map(string)
  default = {
    ManagedBy = "terraform"
  }
}
