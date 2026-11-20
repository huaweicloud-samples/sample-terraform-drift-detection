"""风险分级函数（FunctionGraph Python 运行时入口）。

职责：读取一份巡检结果，对其中每条资源变更判风险等级，
把结论返回给调用方，并把观测数据上报到自定义指标命名空间。

::

    巡检结果原文 → 归一化 → 逐条分级 → 汇总最高等级与动作建议 → 上报自定义指标 → 返回结论

判级规则见 :mod:`common.classification`，指标名与命名空间见 :mod:`common.metrics`，
两者都必须与巡检大盘的取数配置逐字一致。

运行期配置来自用户数据（键名见 ``infra/locals.tf``）：
``REPORTS_BUCKET``、``PLAN_KEY_PREFIX``、``OBS_ENDPOINT``、``CES_NAMESPACE``、
``CES_DIMENSION``、``CES_DIMENSION_VALUE``、``HW_REGION``、``HW_PROJECT_ID``。

事件字段（全部可选）::

    {
      "plan_key": "plans/20260101T000000Z/drift_result.json",  # 指定巡检结果对象
      "plan": {...},                                            # 直接内联巡检结果，不读桶
      "metric": {"namespace": "...", "dimension_name": "...", "dimension_value": "..."},
      "report_metrics": false                                   # 本地演练时关掉指标上报
    }

失败上报：取不到巡检结果、结果无法解析都会抛异常，函数执行记录为失败；
异步调用链路上的失败由基础设施声明的投递配置转发到告警主题。
"""

import json

from common import classification, plan_parser
from common import runtime as runtime_module
from common.errors import PipelineError
from common.metrics import MetricReporter

ROLE = "classifier"


def main_handler(event, context):
    """函数入口。

    :param event: 分级请求，可由编排函数或控制台测试发起
    :param context: 运行上下文，提供临时凭据与用户数据
    """
    print("=== HANDLER ENTRY: classifier ===", flush=True)
    event = event if isinstance(event, dict) else {}
    rt = runtime_module.build(ROLE, event, context)
    return classify_scan(rt, event)


def classify_scan(runtime, event):
    """执行一次分级，返回结论字典。"""
    config = runtime.config
    logger = runtime.logger

    plan_key, insight = _load_plan(runtime, event)
    changes = insight.actionable_changes()
    verdict = classification.classify(changes)

    result = dict(verdict)
    result.update(
        {
            "plan_key": plan_key,
            "plan_format": insight.format,
            "drift_detected": bool(insight.drift_detected),
            "parsed_change_count": len(insight.changes),
            "actionable_change_count": len(changes),
            "diagnostics": insight.diagnostics,
        }
    )

    if _want_metrics(event):
        reporter = MetricReporter(
            api=runtime.api,
            namespace=_metric_value(event, config, "namespace", "CES_NAMESPACE"),
            dimension_name=_metric_value(event, config, "dimension_name", "CES_DIMENSION"),
            dimension_value=_metric_value(event, config, "dimension_value", "CES_DIMENSION_VALUE"),
            logger=logger,
        )
        ok, reported = reporter.report_classification(verdict)
        result["metrics_reported"] = ok
        result["reported_metrics"] = reported
    else:
        result["metrics_reported"] = False
        result["reported_metrics"] = []

    logger.info(
        "分级完成 severity=%s action=%s message=%s plan=%s format=%s",
        verdict.get("severity"),
        verdict.get("action"),
        verdict.get("message"),
        plan_key,
        insight.format,
    )
    return result


# ----------------------------------------------------------------------
# 内部
# ----------------------------------------------------------------------
def _load_plan(runtime, event):
    """取回巡检结果并解析。

    :returns: ``(对象键, PlanInsight)``；内联结果时对象键为 ``<inline>``
    """
    inline = event.get("plan")
    if isinstance(inline, (dict, list, str, bytes, bytearray)):
        return "<inline>", plan_parser.load(inline)

    prefix = "%s/" % runtime.config.require("PLAN_KEY_PREFIX").rstrip("/")
    plan_key = event.get("plan_key") or runtime.store.latest_result_key(prefix)
    if not plan_key:
        raise PipelineError(
            "报告桶内没有可分级的巡检结果",
            reports_bucket=runtime.config.get("REPORTS_BUCKET"),
            prefix=prefix,
        )
    raw = runtime.store.download_json(plan_key)
    insight = plan_parser.load(raw)
    if insight.format == "unknown":
        raise PipelineError(
            "巡检结果无法解析",
            plan_key=plan_key,
            diagnostics=insight.diagnostics,
        )
    return plan_key, insight


def _want_metrics(event):
    value = event.get("report_metrics")
    return True if value is None else bool(value)


def _metric_value(event, config, event_key, config_key):
    metric = event.get("metric") if isinstance(event.get("metric"), dict) else {}
    if metric.get(event_key):
        return metric[event_key]
    return config.require(config_key)


if __name__ == "__main__":  # 本地手工验证入口：python index.py '<事件 JSON>'
    import sys

    raw_event = sys.argv[1] if len(sys.argv) > 1 else "{}"
    print(json.dumps(main_handler(json.loads(raw_event), None), ensure_ascii=False, indent=2))
