"""漂移巡检函数的公共能力层。

本包只提供「与华为云服务交互的最小封装」，不含业务判断：

- :mod:`common.config`        运行期配置读取（事件字段 > 用户数据 > 环境变量）
- :mod:`common.credentials`   运行凭据获取（委托临时凭证优先）
- :mod:`common.signer`        密钥签名请求的规范化与签名
- :mod:`common.httpclient`    带超时与重试上限的 HTTP 传输
- :mod:`common.cloud_api`     云监控 / 消息通知 / 函数工作流 / 构建服务的接口封装
- :mod:`common.obs_store`     巡检报告桶的读取
- :mod:`common.plan_parser`   巡检结果（Terraform 计划输出）解析
- :mod:`common.classification`资源变更的风险分级规则
- :mod:`common.metrics`       自定义指标上报
- :mod:`common.notify`        告警与人工确认通知

所有外部调用点都由调用方显式给出超时与重试上限，失败时抛出
:class:`common.errors.PipelineError`，由函数入口统一转成通知与结构化返回值。
"""

__all__ = [
    "classification",
    "cloud_api",
    "config",
    "credentials",
    "errors",
    "httpclient",
    "metrics",
    "notify",
    "obs_store",
    "plan_parser",
    "runtime",
    "signer",
]
