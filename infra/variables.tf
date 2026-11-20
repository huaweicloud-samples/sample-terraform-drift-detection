# =============================================================================
# 输入变量
# =============================================================================

variable "region" {
  description = "华为云区域。本实践需要 CodeArts 构建机，故默认落在已开通构建机的 cn-east-3"
  type        = string
  default     = "cn-east-3"
}

variable "name_prefix" {
  description = "所有资源的名称前缀"
  type        = string
  default     = "tf-drift"
}

variable "notification_email" {
  description = "接收漂移告警与审批提醒的邮箱地址"
  type        = string
}

variable "schedule_cron" {
  description = "漂移巡检的 6 段 Cron 表达式（秒 分 时 日 月 周）"
  type        = string
  default     = "0 0 */6 * * ?"
}

variable "function_runtime" {
  description = "函数运行时版本"
  type        = string
  default     = "Python3.9"
}

variable "orchestrator_package" {
  description = "编排函数代码包（由 scripts/package_functions.py 生成）"
  type        = string
  default     = "../src/functions/build/orchestrator.zip"
}

variable "classifier_package" {
  description = "分类函数代码包（由 scripts/package_functions.py 生成）"
  type        = string
  default     = "../src/functions/build/classifier.zip"
}

variable "target_infra_package" {
  description = "受管资源代码包，构建任务的巡检与修复对象"
  type        = string
  default     = "../build/target-infra.zip"
}

variable "ces_metric_namespace" {
  description = "自定义指标命名空间，须为 service.item 格式（段首为字母，仅含字母、数字与下划线，总长 3-32，且不以 SYS/AGT/SRE 开头），并与监控大盘查询的命名空间一致"
  type        = string
  default     = "drift_detection.custom"
}

variable "enable_tracing" {
  description = "是否为函数开启调用链追踪（依赖 APM 接入密钥）。目标区域须已上架 APM，否则保持 false"
  type        = bool
  default     = false
}

variable "trigger_enabled" {
  description = "是否启用定时巡检触发器"
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "日志流保留天数"
  type        = number
  default     = 365
}

variable "report_retention_days" {
  description = "巡检报告对象的保留天数"
  type        = number
  default     = 90
}

# -----------------------------------------------------------------------------
# 流水线执行环境（CodeArts 构建）
#
# 以下三个参数取值于租户自己的 CodeArts 环境：构建插件模块 ID 与代码源类型
# 属于账号级目录数据，无法在模块内预置，须在控制台「CodeArts 构建」中查询后填入。
# -----------------------------------------------------------------------------

variable "codearts_project_type" {
  description = "CodeArts 项目类型，合法值：scrum、xboard、basic、phoenix"
  type        = string
  default     = "scrum"

  validation {
    # 实测 service 侧只接受这四个取值，传 common 会在 plan 阶段就报
    # expected type to be one of ["scrum" "xboard" "basic" "phoenix"], got common。
    condition     = contains(["scrum", "xboard", "basic", "phoenix"], var.codearts_project_type)
    error_message = "CodeArts 项目类型只支持 scrum、xboard、basic、phoenix。"
  }
}

variable "build_scm_type" {
  description = "构建任务的代码源类型。实测合法值只有 codehub、gitee、github、gerrit；本实践通常用 codehub"
  type        = string

  validation {
    # 实测：codehub / gitee / github / gerrit 可创建；obs / svn / codearts / custom 均报
    # 非法参数 scms.scm_type。AWS 原实践用的是 CodeBuild 的 S3 源，CodeArts 没有对应形态，
    # 因此这里必须指向一个真实存在的代码仓库。
    condition     = contains(["codehub", "gitee", "github", "gerrit"], var.build_scm_type)
    error_message = "build_scm_type 只支持 codehub、gitee、github、gerrit。"
  }
}

variable "build_scm_url" {
  description = "构建任务源码地址，必须是一个真实存在的代码仓库（CodeArts 不支持对象存储作为构建源）。地址不可达时创建报 DEV-23-50527 仓库不存在"
  type        = string

  validation {
    condition     = can(regex("^https?://", var.build_scm_url))
    error_message = "build_scm_url 必须是 http(s) 开头、指向真实代码仓库的地址。"
  }
}

variable "build_step_module_id" {
  description = "构建步骤的插件模块 ID，取值见 CodeArts 构建控制台的插件目录"
  type        = string
}

variable "build_machine_arch" {
  description = "构建机 CPU 架构，同时决定巡检与修复构建从报告桶取用的离线工具包架构段"
  type        = string
  default     = "x86-64"

  validation {
    # CodeArts 构建接口只接受 x86-64 与 arm，传 x86_64 / arm64 会报
    # CB.00060218 arch参数错误（实测）。离线工具包的架构段由 locals 另行映射，
    # 与这里刻意使用不同的取值空间。
    condition     = contains(["x86-64", "arm"], var.build_machine_arch)
    error_message = "构建机架构只支持 x86-64 与 arm，且报告桶 tools/ 前缀下必须有对应架构的离线工具包与插件镜像。"
  }
}

variable "build_machine_flavor" {
  description = "构建机规格，取值见 CodeArts 构建控制台的规格列表；属固定规格，必须显式填写，不从数据源列表下标推断"
  type        = string
}

variable "terraform_version" {
  description = "构建环境内使用的 Terraform 版本，需与离线工具包一致"
  type        = string
  default     = "1.9.8"
}

variable "plan_build_step_properties" {
  description = "巡检构建步骤的插件参数，随所选插件而异"
  type        = map(string)
  default     = {}
}

variable "apply_build_step_properties" {
  description = "修复构建步骤的插件参数，随所选插件而异"
  type        = map(string)
  default     = {}
}

variable "orchestrator_memory_size" {
  description = "编排函数内存（MB）"
  type        = number
  default     = 256
}

variable "classifier_memory_size" {
  description = "分类函数内存（MB）"
  type        = number
  default     = 256
}

variable "classifier_timeout" {
  description = "分类函数执行超时（秒）"
  type        = number
  default     = 60
}

# -----------------------------------------------------------------------------
# 监控大盘
#
# 下面三个变量的合法取值已在 description 中逐项列出，均为云监控大盘的固定枚举。
# -----------------------------------------------------------------------------

variable "enable_dashboard" {
  description = "是否创建漂移巡检监控大盘"
  type        = bool
  default     = true
}

variable "dashboard_widget_view" {
  description = "组件图表样式，合法值：bar（柱状）、line（折线）、bar_chart（直方图）、table（表格）、circular_bar（饼图）、area_chart（面积图）"
  type        = string
  default     = "line"
}

variable "dashboard_metric_display_mode" {
  description = "单个组件展示的指标数量，合法值：single（一条指标）、multiple（多条指标）"
  type        = string
  default     = "multiple"
}

variable "dashboard_dimension_filter_type" {
  description = "组件维度筛选方式，合法值：all_instances（全部资源）、specific_instances（指定维度值）"
  type        = string
  default     = "specific_instances"
}

variable "metric_dimension_name" {
  description = "自定义指标维度名称，须与函数上报时使用的维度名一致"
  type        = string
  default     = "Pipeline"
}

variable "metric_dimension_value" {
  description = "自定义指标维度取值，须与函数上报时使用的维度值一致"
  type        = string
  default     = "drift-detection"
}

variable "build_wait_seconds" {
  description = "编排函数等待单次构建完成的最长秒数"
  type        = number
  default     = 900
}

variable "build_poll_interval_seconds" {
  description = "编排函数轮询构建状态的间隔秒数"
  type        = number
  default     = 15
}

# -----------------------------------------------------------------------------
# 函数运行权限委托
# -----------------------------------------------------------------------------

variable "agency_domain_roles" {
  description = "委托绑定的全局级（AX）角色。IAM 角色分 AX / XA 两类，授错层级时委托创建成功但角色不进 token，调用才报 403/422"
  type        = list(string)
  default = [
    # OBS 是全局级服务，桶与对象操作需要 AX 角色
    "OBS Administrator",
    # 触发 CodeArts 构建需要该角色；它在 IAM 里是 AX（全局级），
    # 放进 project_role 不生效，函数调用时报 CB.032101 权限不足。
    # 注意：该角色覆盖账号下所有项目的构建操作，属较宽授权。
    "CodeArtsBuildAgencyFullPolicy",
  ]
}

variable "agency_project_roles" {
  description = "委托绑定的项目级（XA）角色。角色名须与 IAM 权限列表完全一致，写错时 IAM 不报错但角色不进 token"
  type        = list(string)
  default = [
    "FunctionGraph FullAccess",
    # SMN 在 IAM 里没有 FullAccess 角色（只有 ReadOnlyAccess，不能发消息）。
    # 这里用最小发布权限角色，其策略恰为 smn:topic:publish。
    # 缺它时异步调用配置报 FSS.1085 please configure SMN operate permissions。
    "CTSAgencySmnPolicy",
    # CES 的角色名带 Policy 后缀，写成 "CES FullAccess" 不存在
    "CES FullAccessPolicy",
    # KMS 的对象解密（桶加密对象读取）需要项目级 KMS 权限，
    # 只给全局级 "KMS CMK Decrypter" 时会报 CSMS.0401 / 对象读取 403
    "KMS CMKFullAccess",
    # 函数日志写入 LTS
    "LTS FullAccess",
  ]
}

variable "tags" {
  description = "为所有资源追加的标签"
  type        = map(string)
  default = {
    ManagedBy = "terraform"
    Pattern   = "drift-detection"
  }
}
