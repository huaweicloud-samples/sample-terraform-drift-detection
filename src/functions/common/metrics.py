"""自定义指标上报。

指标名、命名空间与维度名必须与巡检大盘的取数配置逐字一致，
否则大盘查不到数据，因此这三个要素在本文件里集中声明，其余模块只引用常量。

上报口径（每次巡检恰好落一组数据点）：

- 无差异：``NoDrift`` = 1
- 有差异：``DriftDetected`` = 1、``DriftResourceCount`` = 差异资源数、
  并按最高风险等级上报 ``Drift_Critical`` / ``Drift_High`` / ``Drift_Low`` = 1

指标上报是观测面而非控制面：失败只记日志并在返回值里标记
``metrics_reported=false``，绝不因为「图上看不到点」而让巡检结论作废。
"""

from common.classification import (
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_NONE,
)

METRIC_DRIFT_DETECTED = "DriftDetected"
METRIC_NO_DRIFT = "NoDrift"
METRIC_RESOURCE_COUNT = "DriftResourceCount"
SEVERITY_METRICS = {
    SEVERITY_CRITICAL: "Drift_Critical",
    SEVERITY_HIGH: "Drift_High",
    SEVERITY_LOW: "Drift_Low",
}
METRIC_UNIT = "Count"


class MetricReporter(object):
    """把分级结论写成云监控自定义指标。"""

    def __init__(self, api, namespace, dimension_name, dimension_value, logger=None):
        self.api = api
        self.namespace = namespace
        self.dimension_name = dimension_name
        self.dimension_value = dimension_value
        self.logger = logger

    def report_classification(self, verdict):
        """按分级结论上报数据点，返回 ``(是否全部成功, 已上报指标名列表)``。

        :param verdict: :func:`common.classification.classify` 的返回值
        """
        severity = str(verdict.get("severity") or SEVERITY_NONE)
        points = []
        if severity == SEVERITY_NONE:
            points.append((METRIC_NO_DRIFT, 1))
        else:
            counts = verdict.get("counts") or {}
            points.append((METRIC_DRIFT_DETECTED, 1))
            points.append((METRIC_RESOURCE_COUNT, int(counts.get("total") or 0)))
            metric_name = SEVERITY_METRICS.get(severity)
            if metric_name:
                points.append((metric_name, 1))

        reported = []
        all_ok = True
        for name, value in points:
            if self._safe_report(name, value):
                reported.append(name)
            else:
                all_ok = False
        return all_ok, reported

    def _safe_report(self, metric_name, value):
        dimensions = {self.dimension_name: self.dimension_value}
        try:
            self.api.report_metric(
                namespace=self.namespace,
                metric_name=metric_name,
                value=value,
                dimensions=dimensions,
                unit=METRIC_UNIT,
            )
            return True
        except Exception as error:  # 观测面失败不影响巡检结论
            if self.logger is not None:
                self.logger.warning(
                    "自定义指标上报失败 metric=%s namespace=%s detail=%s",
                    metric_name,
                    self.namespace,
                    error,
                )
            return False
