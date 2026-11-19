terraform {
  required_version = ">= 1.5"

  required_providers {
    huaweicloud = {
      source  = "huaweicloud/huaweicloud"
      version = "~> 1.96.1"
    }
  }
}

provider "huaweicloud" {
  region = var.region

  # Access Key 与 Secret Key 在华为云控制台「我的凭证」页面获取后填入部署脚本，
  # 运行区域与项目 ID 同样由部署脚本给出。本文件不写入任何密钥内容。
}
