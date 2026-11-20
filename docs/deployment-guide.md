# 部署指南

本文给出从零到可验证部署的完整命令，每一步都附预期输出。
命令默认在本仓库根目录执行，需要进入子目录时会显式写出 `cd`。

架构与设计取舍见 [architecture.md](architecture.md)。

## 0. 约定与占位符

| 占位符 | 含义 | 取值方式 |
|-|-|-|
| `<HW_ACCESS_KEY>` | Access Key ID | 华为云控制台「我的凭证」页面获取 |
| `<HW_SECRET_KEY>` | Secret Access Key | 同上；两者只填入部署脚本，不要写入代码或提交到仓库 |
| `<报告桶名>` | 巡检报告桶 | 部署完成后执行 `terraform output reports_bucket` |
| `<构建机规格>` 等 | CodeArts 构建环境的取值 | CodeArts 构建控制台的新建任务页面查询 |

凭据在本实践中有两处用途，彼此独立：

- 创建巡检环境：由执行 `terraform` 的进程读取 `HW_ACCESS_KEY`、`HW_SECRET_KEY`
  （使用临时安全令牌时另加 `HW_SECURITY_TOKEN`）。
- 运行期函数调用云服务：由函数绑定的委托按调用签发临时授权，函数代码与配置里没有长期密钥。

## 1. 自检本地工具

```bash
python --version
terraform version
```

预期输出（版本不低于下表要求即可）：

```text
Python 3.11.9
Terraform v1.9.8
```

要求：Terraform CLI `>= 1.5`，huaweicloud Provider `~> 1.96.1`，Python 3.9 及以上。
本实践在 `cn-east-3` 验证，区域不是 cn-east-3 时需同步调整 `var.region` 与命名空间取值。

## 2. 构建代码包

```bash
python scripts/package_functions.py
```

预期输出：

```text
[OK] orchestrator src/functions/build/orchestrator.zip  条目 15  sha256 e330ab31b9b7  大小 36 KB
[OK] classifier   src/functions/build/classifier.zip  条目 15  sha256 455c25ec8a22  大小 34 KB
[OK] target-infra build/target-infra.zip  条目 3  sha256 66d19084ad07  大小 2 KB
```

说明：

- `sha256` 由源码内容决定，与上例不同属正常现象；条目数固定为 15、15、3。
- 三个产物路径与 `infra/variables.tf` 的 `orchestrator_package`、`classifier_package`、
  `target_infra_package` 默认值逐字一致，因此部署时无需再传路径。
- 构建过程只做本地校验：逐个编译源码、检查 `index.py` 是否位于包内根目录、
  拦截运行时内置模块与原生扩展混入，不访问任何云端接口。

只构建部分产物：

```bash
python scripts/package_functions.py --only orchestrator classifier
python scripts/package_functions.py --only target-infra
```

同一入口的薄封装（会优先探测 `python3`）：

```bash
bash src/functions/build.sh
bash src/functions/build.sh orchestrator
```

校验失败时的预期输出（退出码 1）：

```text
[FAIL] orchestrator: 包内根目录缺少 index.py，函数处理器 index.main_handler 无法加载

构建未完成：包内根目录缺少 index.py，函数处理器 index.main_handler 无法加载
```

其余失败原因包括：语法错误（`语法错误：<文件>（行 N）<消息>`）、能力层模块缺失
（`能力层缺少模块：…`）、禁止条目混入（`禁止打包原生扩展或字节码：…`、
`禁止把运行时内置或不使用的模块打进包：…`）。

## 3. 本地静态校验

```bash
python -m pytest src/functions -q
```

预期输出：

```text
131 passed in 0.24s
```

按模块单独跑，便于定位问题：

| 命令 | 预期 |
|-|-|
| `python -m pytest src/functions/tests/test_plan_parser.py -q` | `26 passed` |
| `python -m pytest src/functions/tests/test_cloud_api.py -q` | `25 passed` |
| `python -m pytest src/functions/tests/test_classification.py -q` | `20 passed` |
| `python -m pytest src/functions/tests/test_signer.py -q` | `20 passed` |
| `python -m pytest src/functions/tests/test_config_credentials.py -q` | `17 passed` |
| `python -m pytest src/functions/tests/test_notify.py -q` | `10 passed` |
| `python -m pytest src/functions/tests/test_metrics.py -q` | `9 passed` |
| `python -m pytest src/functions/tests/test_infra_contract.py -q` | `4 passed` |

其中 `test_infra_contract.py` 校验函数侧使用的运行期配置键名与基础设施声明是否一致，
改动 `infra/locals.tf` 后应先跑这一条。

```bash
cd infra
terraform validate
```

预期输出：

```text
Success! The configuration is valid, but there were some
validation warnings as shown above.
```

未配置华为云官方插件镜像时，警告内容是本地开发覆盖提示，属预期。

受管资源目录同样可以单独校验：

```bash
cd ../target-infra
terraform init -backend=false -input=false
terraform validate
cd ../infra
```

预期输出为 `Success!`。`-backend=false` 用于跳过后端初始化：受管资源声明的是本地后端，
状态文件在构建机内生成，本地校验不需要也不应该产生状态。

## 4. 配置访问凭据

在华为云控制台「我的凭证」页面的「访问密钥」区域获取 Access Key ID 与 Secret Access Key，
然后填入当前 shell：

```bash
export HW_ACCESS_KEY=<HW_ACCESS_KEY>
export HW_SECRET_KEY=<HW_SECRET_KEY>
```

使用临时安全令牌时另加：

```bash
export HW_SECURITY_TOKEN=<HW_SECURITY_TOKEN>
```

`infra/providers.tf` 不写入任何密钥内容，凭据由进程环境读取；
区域与项目 ID 由 `data.huaweicloud_account.current` 自动取得。

## 5. 填写部署参数

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
```

`terraform.tfvars` 已在 `.gitignore` 中忽略，不会被提交。
必填项只有 5 个，缺任何一个 `terraform plan` 都会报错并指名该变量：

| 必填项 | 填写内容 |
|-|-|
| `notification_email` | 接收漂移告警的邮箱；订阅创建后需在邮件中点击确认，否则不投递 |
| `build_machine_flavor` | CodeArts 构建控制台新建任务页面的规格下拉取值，属固定规格 |
| `build_scm_type` | 同一控制台的代码源类型下拉取值 |
| `build_scm_url` | 使用内置代码包时，填 OBS 中源码归档的访问地址 |
| `build_step_module_id` | 构建步骤所选插件的模块 ID |

其余变量按 `README.md` 的部署参数表取值即可。几个容易填错的位置：

- `schedule_cron` 为 6 段（秒 分 时 日 月 周），「日」与「周」不能同时为 `*`，
  默认 `0 0 */6 * * ?` 表示每 6 小时一次。
- `ces_metric_namespace` 必须是 `service.item` 形式：仅字母、数字与下划线、总长 3-32、
  不以 `SYS`、`AGT`、`SRE` 开头、不含连字符。
- `metric_dimension_name` / `metric_dimension_value` 必须与大盘取数一致，默认
  `Pipeline` / `drift-detection`。
- `plan_build_step_properties` 与 `apply_build_step_properties` 随所选插件不同；
  使用 Shell 脚本类插件时按示例填 `{ SCRIPT_PATH = "buildscripts/plan.sh" }` 与
  `{ SCRIPT_PATH = "buildscripts/apply.sh" }`。
- `agency_domain_roles` 与 `agency_project_roles` 分属全局级与项目级两个层级。
  授错层级时委托创建成功、但角色不进临时授权，只有调用才报权限不足。

## 6. 初始化并创建巡检环境

```bash
terraform init
```

预期结尾输出：

```text
Terraform has been successfully initialized!
```

```bash
terraform plan -out=drift.tfplan
```

预期输出（两个可选开关均为默认 `true` 时）：

```text
Plan: 28 to add, 0 to change, 0 to destroy.
```

28 个资源的构成：2 个函数、1 个定时触发器、1 个异步调用配置、1 个 APM 接入密钥、
2 个调用链追踪配置、1 个 CodeArts 项目、2 个构建任务、1 个报告桶、3 个桶对象、
1 个公开访问阻断、2 条桶策略、1 个主密钥、1 个主题、1 个邮件订阅、
1 个日志组、2 个日志流、1 个委托、1 个大盘、3 个大盘组件。

把 `enable_dashboard` 或 `enable_tracing` 置为 `false` 时对应资源不创建，
全部关闭为 21 个。

```bash
terraform apply drift.tfplan
```

预期输出：

```text
Apply complete! Resources: 28 added, 0 changed, 0 destroyed.
```

`terraform.tfvars.example` 中的构建参数取自你自己的租户环境，若 `module_id` 或规格填写有误，
构建任务会在创建步骤报错，此时修正变量后重跑 `terraform apply` 即可。

## 7. 读取部署输出

```bash
terraform output
```

预期列出以下输出名（与 `infra/outputs.tf` 完全一致）：

```text
agency_name
alert_topic_urn
apply_build_job_id
classifier_function_urn
dashboard_id
encryption_key_id
log_group_name
managed_state_object_key
orchestrator_function_urn
plan_build_job_id
plan_object_prefix
reports_bucket
scan_cron
source_object_key
```

常用取值：

```bash
terraform output reports_bucket              # 报告桶名，第 8 步上传工具链要用
terraform output plan_build_job_id           # 巡检构建任务 ID，控制台手动触发时使用
terraform output apply_build_job_id          # 修复构建任务 ID
terraform output alert_topic_urn             # 告警主题 URN
terraform output encryption_key_id           # 主密钥 ID，受管资源的 assets_kms_key_id 用它
terraform output managed_state_object_key    # 状态对象名，固定 state/target-infra.tfstate
terraform output source_object_key           # 受管资源代码包对象名，固定 source/target-infra.zip
terraform output plan_object_prefix          # 巡检结果前缀，固定 plans
terraform output log_group_name              # 日志组名，形如 tf-drift-logs
terraform output agency_name                 # 委托名，形如 tf-drift-cff-agency
terraform output scan_cron                   # 当前生效的巡检周期表达式
terraform output dashboard_id                # 大盘 ID；enable_dashboard 为 false 时是空串
```

函数 URN 形如 `urn:fss:<区域>:<项目 ID>:function:<组>:<名>:latest`，
引用时一律取 `.urn`，不带版本号后缀。

## 8. 上传离线工具链（手工前置动作）

构建脚本只从内网对象存储取包，桶内必须先存在以下层级，否则首次巡检构建会在准备工具链阶段失败：

```text
obs://<报告桶名>/tools/terraform-<TERRAFORM_VERSION>-linux-amd64.tar.gz
obs://<报告桶名>/tools/provider-mirror/registry.terraform.io/huaweicloud/huaweicloud/<版本>/linux_amd64/…
```

```bash
obsutil cp ./terraform-1.9.8-linux-amd64.tar.gz obs://<报告桶名>/tools/terraform-1.9.8-linux-amd64.tar.gz
obsutil cp ./provider-mirror/ obs://<报告桶名>/tools/provider-mirror/ -r
obsutil ls obs://<报告桶名>/tools/ -s
```

预期输出：前两条命令执行完成、无错误提示；`ls` 能列出刚上传的对象。

要点：

- 文件名里的版本必须等于部署时 `var.terraform_version` 的取值（默认 `1.9.8`），
  构建脚本按 `tools/terraform-${TERRAFORM_VERSION}-linux-amd64.tar.gz` 取对象。
- 解压后 Terraform 可执行文件要位于压缩包根目录，构建脚本会解压到工作区的 `.tools/bin`。
- provider 镜像目录的层级不能改动：构建脚本生成的插件镜像配置以
  `registry.terraform.io/*/*` 匹配 `filesystem_mirror` 路径。
- 构建机 CPU 架构与包名后缀需匹配；`var.build_machine_arch` 默认 `x86_64`，
  改用其它架构需同步准备对应的安装包。

## 9. 落地受管资源并写入初始状态

受管资源的状态文件放在报告桶的 `state/target-infra.tfstate`。桶内没有状态文件时，
巡检会把全部资源视为待创建，因此首次部署要先落一次状态。

方式一（推荐，顺带验证修复构建可用）：在 CodeArts 构建控制台手动触发
`tf-drift-apply` 任务。该任务的参数 `PIPELINE_MODE` 取值 `apply`，
`buildscripts/apply.sh` 会执行 `terraform apply -input=false -auto-approve`，
随后把状态写回 `STATE_KEY`。预期在构建日志中看到：

```text
Apply complete! Resources: 8 added, 0 changed, 0 destroyed.
[<时间>] 最新状态文件已写回 state/target-infra.tfstate
```

受管资源共 8 个：1 个存储桶、1 条桶策略、1 个安全组、2 条安全组规则、
1 个用户组、1 个自定义权限、1 条用户组权限授予。

方式二，手工上传本地状态：

```bash
obsutil cp ./target-infra/terraform.tfstate obs://<报告桶名>/state/target-infra.tfstate
```

受管资源的两个必填变量：`assets_bucket`（全局唯一桶名）与 `assets_kms_key_id`
（取 `terraform output encryption_key_id`）。

## 10. 验证巡检链路

### 10.1 演练一次巡检（不动任何资源）

在函数工作流控制台对 `tf-drift-orchestrator` 执行「测试函数」，事件填入：

```json
{
  "dry_run": true,
  "wait_seconds": 60,
  "poll_interval": 5
}
```

预期函数日志首行：

```text
=== HANDLER ENTRY: orchestrator ===
```

中间按阶段推进，预期出现：

```text
[INFO] request_id=<请求编号> 已触发巡检构建 job_id=<任务 ID> build_no=<构建编号>
[INFO] request_id=<请求编号> 构建结束 job_id=<任务 ID> build_no=<构建编号> status=success duration=<耗时>
[INFO] request_id=<请求编号> 取到最新巡检结果对象 key=plans/<时间戳>/drift_result.json
[INFO] request_id=<请求编号> 演练模式：跳过修复构建
```

每行前面还有一个时间戳字段，此处省略。构建状态的轮询口径：命中 `success`、`successed`、
`successful`、`failed`、`fail`、`error`、`canceled`、`cancelled`、`stopped`、`abort`、
`aborted`、`timeout` 之一即为终态，其余取值一律视为「仍在执行」直到 `build_wait_seconds` 超时；
判定成功的取值为前三个。

结尾一行给出本次结论：

```text
[INFO] request_id=<请求编号> 巡检结束 status=remediated severity=low message=共 1 项差异：严重 0、高 0、低 1
```

`status` 的完整取值：`no_drift`（无差异）、`remediated`（已自动修复）、
`pending_approval`（已发通知等待确认）、`would_remediate` / `would_notify`（演练模式跳过动作）、
`failed`（链路异常，返回值同时带 `failed_stage`）。

返回值里的 `stages` 数组按顺序记录走过的阶段：`plan_build`、`read_plan`、`classify`、`act`。

### 10.2 单独验证分级函数

对 `tf-drift-classifier` 填入事件（关闭指标上报，适合本地演练）：

```json
{
  "plan_key": "plans/<时间戳>/drift_result.json",
  "report_metrics": false
}
```

预期日志：

```text
=== HANDLER ENTRY: classifier ===
[INFO] request_id=<请求编号> 分级完成 severity=low action=auto_remediate message=共 1 项差异：严重 0、高 0、低 1 plan=plans/<时间戳>/drift_result.json format=json
```

返回结构包含 `severity`、`action`、`message`、`counts`、`critical_changes`、`high_changes`、
`low_changes`、`plan_key`、`plan_format`、`drift_detected`、`parsed_change_count`、
`actionable_change_count`、`diagnostics`、`metrics_reported`、`reported_metrics`。
`action` 只有三个取值：`none`、`auto_remediate`（仅低风险）、`notify_and_approve`（严重与高风险）。

无差异时 `message` 为 `未发现需要处理的配置差异`，`severity` 为 `none`。

也可以内联一份巡检结果，完全不读桶：把样例内容放进事件的 `plan` 字段。
可用夹具在 `src/functions/tests/fixtures/`：
`plan_with_drift.json`（含漂移的结构化计划）、`critical_only.json`（纯严重）、
`low_risk_only.json`（纯低风险）、`no_drift.json`（无差异）、
`state_drift_only.json`（仅状态差异）、`plan_stream.jsonl`（逐行事件流）。

### 10.3 验证通知链路

```bash
terraform output alert_topic_urn
```

1. 在 SMN 控制台确认邮箱订阅已完成确认。
2. 制造一次严重变更：删除受管资源安全组的 443 入方向规则，或修改桶策略。
3. 触发一次巡检构建或等待下个周期。

预期收到邮件，主题：

```text
Terraform Drift Approval Required
```

正文前两行：

```text
巡检发现需要人工确认的配置差异。
风险等级：critical
```

随后是差异条数、严重与高风险变更明细（超过 20 条会折叠并提示其余条目所在对象）、
巡检结果对象、巡检构建任务与编号、报告桶名，以及确认修复的方式：

```text
确认修复的方式：
  执行命令：terraform apply -auto-approve
  或触发修复构建任务：<apply 构建任务 ID>
  未经确认不会自动改动上述资源。
```

链路故障（构建未成功、桶内无可判读结果、分级返回结构不完整等）时收到主题
`Terraform Drift Pipeline Failed` 的邮件，正文带失败阶段与构建编号。

### 10.4 验证巡检大盘

```bash
terraform output dashboard_id
```

云监控控制台打开 `tf-drift-dashboard`，三个组件的预期取数：

| 组件 | 指标 |
|-|-|
| 巡检结论 | `DriftDetected`、`NoDrift` |
| 变更风险分级 | `Drift_Critical`、`Drift_High`、`Drift_Low` |
| 漂移资源数 | `DriftResourceCount` |

命名空间与维度分别取 `ces_metric_namespace`、`metric_dimension_name`、
`metric_dimension_value` 的部署值。无数据时优先核对这三处是否与函数上报取值完全一致。

### 10.5 验证留痕与日志

```bash
obsutil ls obs://<报告桶名>/ -r | head
```

预期能看到四类前缀：`plans/`（每次巡检结果）、`applies/`（修复执行摘要）、
`state/target-infra.tfstate`（最新状态）、`source/` 与 `functions/`（代码包）。

```bash
obsutil cat obs://<报告桶名>/plans/<时间戳>/drift_result.json
```

预期是 Terraform 结构化计划原文，含 `resource_changes` 数组；
无差异时构建脚本写入的是：

```json
{"drift_detected": false, "resource_changes": []}
```

日志侧：

```bash
terraform output log_group_name
```

在云日志服务控制台打开该日志组，其下两个日志流 `<name_prefix>-orchestrator` 与
`<name_prefix>-classifier` 分别对应两个函数；每条日志带本次调用的请求编号。

### 10.6 验证定时巡检本身

```bash
terraform output scan_cron
```

在函数工作流控制台查看触发器列表，确认 `tf-drift-orchestrator` 的定时触发器状态为 `ACTIVE`，
周期与 `scan_cron` 输出一致。若 `trigger_enabled = false`，创建出来即为 `DISABLED`。

**部署完成后请按本节步骤在真实环境自行验证，确认功能可用后方可视为交付完成。**

## 11. 日常运维动作

### 11.1 更新函数代码

```bash
python scripts/package_functions.py --only orchestrator classifier
cd infra
terraform apply
```

代码包对象变化会触发函数重建（配置里声明了 `replace_triggered_by`），
否则函数仍运行旧包。预期输出包含函数的替换与重建。

### 11.2 调整巡检周期

定时触发器的周期字段为强制新建项，且生效中的触发器不支持原地改写：

```bash
cd infra
terraform destroy -target=huaweicloud_fgs_trigger.scheduled_scan
terraform apply
```

修改 `var.schedule_cron` 后按上述顺序重建即可。

### 11.3 临时停掉巡检

触发器的 `status` 与周期都已在配置里让出所有权，`trigger_enabled` 的改动不会产生差异。
停巡检请在函数工作流控制台把触发器置为 `DISABLED`（取值只有 `ACTIVE` / `DISABLED`），
调整日志保留期、构建插件版本同理，控制台上的改动不会被下一次巡检计划改回去。

### 11.4 重新加载受管资源代码

改动 `target-infra/` 后：

```bash
python scripts/package_functions.py --only target-infra
cd infra
terraform apply
```

`source/target-infra.zip` 对象更新后，构建脚本会在工作区没有 `main.tf` 时从桶内取回代码包并解压。
包内条目统一带 `target-infra/` 前缀，与构建脚本的
`TARGET_DIR="${WORKSPACE}/target-infra"` 对齐，这一点不要手工改包结构。

## 12. 释放资源

```bash
cd infra
terraform destroy
```

预期输出：

```text
Destroy complete! Resources: 28 destroyed.
```

报告桶声明了 `force_destroy = true` 并开启版本控制，销毁时会连带清理历史版本对象与
`tools/` 前缀下的离线工具链；重建环境时需要重新上传。

受管资源在构建机内由修复任务管理，释放它们请对 `target-infra/` 单独执行
`terraform destroy`（把桶内取回的状态文件放到该目录后执行），随后清理桶内的状态对象。

释放前建议先确认桶内已无需要留档的巡检报告：

```bash
obsutil ls obs://<报告桶名>/plans/ -r
```

## 13. 故障排查

| 现象 | 定位方向 |
|-|-|
| 函数报「找不到入口」 | 代码包根目录必须有 `index.py`，处理器为 `index.main_handler`；只用 `scripts/package_functions.py` 的产物 |
| 构建在准备工具链阶段失败 | `tools/` 前缀缺少与 `TERRAFORM_VERSION` 同名的包，或对象层级与第 8 步不一致 |
| 构建报 `terraform init` 无法安装插件 | provider 镜像目录层级不符，`filesystem_mirror` 匹配不到 `registry.terraform.io/*/*` |
| 函数调用返回权限不足 | 委托角色层级放错（全局级 / 项目级）；对象存储读写还需桶策略放行，两者分别由 `infra/iam.tf`、`infra/obs.tf` 声明 |
| 巡检构建失败但收不到故障邮件 | 邮箱订阅未确认；`alert_topic_urn` 与主题实际 URN 不一致 |
| 大盘无数据 | 命名空间、指标名或维度名与函数上报不一致；命名空间含连字符或以 `SYS`、`AGT`、`SRE` 开头会被拒绝写入 |
| 每次计划都提示构建步骤变更 | 改动的不只是 `steps[0].version`，核对 `module_id` 与步骤参数 |
| 首次巡检报「全部资源待创建」 | 桶内没有 `state/target-infra.tfstate`，按第 9 步落一次状态 |
| 分级函数把纯标签变更判成高风险 | 计划输出含未确定空值属正常处理范围，若仍误判请核对夹具与实际计划差异，检查 `common/classification.py` 的清单 |
| 调整巡检周期后没生效 | 周期字段为强制新建项，需先销毁触发器再重建（见 11.2） |
