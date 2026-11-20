# =============================================================================
# 巡检执行环境：CodeArts 构建
#
# 两个构建任务分别承载「巡检（terraform plan）」与「修复（terraform apply）」。
# 构建任务只定义执行环境与参数，具体命令由源码包内的脚本给出。
#
# 说明：构建机规格、代码源类型、构建步骤插件模块 ID 都属于租户环境内的目录数据，
# 无法在模块里预置，作为输入变量由使用者在 CodeArts 控制台查询后填入。
# =============================================================================

resource "huaweicloud_codearts_project" "pipeline" {
  name        = "${var.name_prefix}-pipeline"
  description = "漂移巡检流水线所属项目"
  type        = var.codearts_project_type
}

locals {
  # 巡检与修复共用的执行环境参数
  build_common_params = {
    REPORTS_BUCKET    = huaweicloud_obs_bucket.reports.bucket
    OBS_ENDPOINT      = local.obs_internal_endpoint
    STATE_KEY         = local.state_object_key
    SOURCE_KEY        = local.source_object_key
    PLAN_KEY_PREFIX   = local.plan_object_prefix
    TERRAFORM_VERSION = var.terraform_version
    TOOL_ARCH         = local.build_tool_arch
    HW_REGION         = var.region
  }
}

resource "huaweicloud_codearts_build_task" "plan" {
  name       = "${var.name_prefix}-plan"
  project_id = huaweicloud_codearts_project.pipeline.id
  arch       = var.build_machine_arch
  flavor     = var.build_machine_flavor

  scms {
    scm_type      = var.build_scm_type
    url           = var.build_scm_url
    is_auto_build = false
  }

  steps {
    name       = "detect-drift"
    module_id  = var.build_step_module_id
    enable     = true
    properties = merge(local.build_common_params, var.plan_build_step_properties)
  }

  parameters {
    name = "PIPELINE_MODE"

    params {
      name  = "PIPELINE_MODE"
      value = "plan"
    }
  }

  lifecycle {
    # 构建插件的版本号由服务侧回填，配置里不声明；忽略后不会出现每次 plan 都有差异的假变更。
    # 改动的只是 steps[0].version，构建步骤名、插件 ID 与参数仍按配置收敛。
    ignore_changes = [steps[0].version]
  }
}

resource "huaweicloud_codearts_build_task" "apply" {
  name       = "${var.name_prefix}-apply"
  project_id = huaweicloud_codearts_project.pipeline.id
  arch       = var.build_machine_arch
  flavor     = var.build_machine_flavor

  scms {
    scm_type      = var.build_scm_type
    url           = var.build_scm_url
    is_auto_build = false
  }

  steps {
    name       = "remediate-drift"
    module_id  = var.build_step_module_id
    enable     = true
    properties = merge(local.build_common_params, var.apply_build_step_properties)
  }

  parameters {
    name = "PIPELINE_MODE"

    params {
      name  = "PIPELINE_MODE"
      value = "apply"
    }
  }

  lifecycle {
    # 与巡检构建任务同理：只忽略服务侧回填的插件版本号。
    ignore_changes = [steps[0].version]
  }
}
