# -*- coding: utf-8 -*-
"""自定义指标上报校验：数据点组合、命名空间与失败隔离。"""

import unittest

from tests import support

from common import metrics
from common.errors import ServiceError


def _reporter(api=None, **overrides):
    kwargs = {
        "api": api or support.FakeApi(),
        "namespace": "drift_detection.custom",
        "dimension_name": "Pipeline",
        "dimension_value": "drift-detection",
    }
    kwargs.update(overrides)
    return metrics.MetricReporter(**kwargs)


class MetricNamingTest(unittest.TestCase):
    def test_names_match_dashboard_declaration(self):
        self.assertEqual("DriftDetected", metrics.METRIC_DRIFT_DETECTED)
        self.assertEqual("NoDrift", metrics.METRIC_NO_DRIFT)
        self.assertEqual("DriftResourceCount", metrics.METRIC_RESOURCE_COUNT)
        self.assertEqual(
            {"critical": "Drift_Critical", "high": "Drift_High", "low": "Drift_Low"},
            metrics.SEVERITY_METRICS,
        )

    def test_namespace_rejects_hyphen(self):
        """自定义命名空间含连字符会被云监控拒收。"""
        self.assertNotIn("-", "drift_detection.custom")
        for segment in "drift_detection.custom".split("."):
            self.assertRegex(segment, r"^[A-Za-z][A-Za-z0-9_]*$")


class DataPointTest(unittest.TestCase):
    def test_no_drift_reports_single_point(self):
        api = support.FakeApi()
        ok, reported = _reporter(api=api).report_classification(
            support.verdict_of("no_drift.json")
        )
        self.assertTrue(ok)
        self.assertEqual(["NoDrift"], reported)
        point = api.metrics[0]
        self.assertEqual(1, point["value"])
        self.assertEqual("Count", point["unit"])
        self.assertEqual({"Pipeline": "drift-detection"}, point["dimensions"])
        self.assertEqual("drift_detection.custom", point["namespace"])

    def test_critical_reports_detection_count_and_severity(self):
        api = support.FakeApi()
        ok, reported = _reporter(api=api).report_classification(
            support.verdict_of("plan_with_drift.json")
        )
        self.assertTrue(ok)
        self.assertEqual(["DriftDetected", "DriftResourceCount", "Drift_Critical"], reported)
        values = {point["metric_name"]: point["value"] for point in api.metrics}
        self.assertEqual(1, values["DriftDetected"])
        self.assertEqual(4, values["DriftResourceCount"])
        self.assertEqual(1, values["Drift_Critical"])

    def test_low_only_reports_low_metric(self):
        api = support.FakeApi()
        _reporter(api=api).report_classification(support.verdict_of("low_risk_only.json"))
        names = [point["metric_name"] for point in api.metrics]
        self.assertEqual(["DriftDetected", "DriftResourceCount", "Drift_Low"], names)

    def test_high_only_reports_high_metric(self):
        api = support.FakeApi()
        _reporter(api=api).report_classification(support.verdict_of("state_drift_only.json"))
        names = [point["metric_name"] for point in api.metrics]
        self.assertIn("Drift_High", names)
        self.assertNotIn("Drift_Critical", names)

    def test_every_point_carries_the_declared_dimension(self):
        api = support.FakeApi()
        _reporter(api=api).report_classification(support.verdict_of("critical_only.json"))
        self.assertEqual(3, len(api.metrics))
        for point in api.metrics:
            self.assertEqual({"Pipeline": "drift-detection"}, point["dimensions"])


class FailureIsolationTest(unittest.TestCase):
    def test_metric_failure_does_not_break_the_verdict(self):
        """观测面失败只记为告警日志，不影响巡检结论。"""
        api = support.FakeApi(metric_error=ServiceError("指标上报被拒收", status_code=400))
        ok, reported = _reporter(api=api).report_classification(
            support.verdict_of("plan_with_drift.json")
        )
        self.assertFalse(ok)
        self.assertEqual([], reported)

    def test_partial_failure_is_reported(self):
        class FlakyApi(support.FakeApi):
            def report_metric(self, **kwargs):
                if kwargs["metric_name"] == "DriftResourceCount":
                    raise ServiceError("单点失败", status_code=500)
                return super().report_metric(**kwargs)

        api = FlakyApi()
        ok, reported = _reporter(api=api).report_classification(
            support.verdict_of("plan_with_drift.json")
        )
        self.assertFalse(ok)
        self.assertEqual(["DriftDetected", "Drift_Critical"], reported)


if __name__ == "__main__":
    unittest.main()
