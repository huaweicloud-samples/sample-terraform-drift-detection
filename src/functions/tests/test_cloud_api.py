# -*- coding: utf-8 -*-
"""接口封装校验：请求路径、请求体、超时与重试参数。

替身传输层记录每一次请求，因此这些断言起到了「上线前逐个核对调用点」的作用，
但不会真的访问任何服务。
"""

import json
import unittest

from tests import support

from common.cloud_api import (
    RETRY_INVOKE_FUNCTION,
    RETRY_METRIC,
    RETRY_PUBLISH,
    RETRY_QUERY_BUILD,
    RETRY_START_BUILD,
    TIMEOUT_INVOKE_FUNCTION,
    TIMEOUT_METRIC,
    TIMEOUT_PUBLISH,
    TIMEOUT_QUERY_BUILD,
    TIMEOUT_START_BUILD,
    CloudApi,
)
from common.errors import ConfigError, ServiceError

CLASSIFIER_URN = (
    "urn:fss:cn-north-4:example-project-id:function:default:tf-drift-classifier:latest"
)
TOPIC_URN = "urn:smn:cn-north-4:example-project-id:tf-drift-alerts"


def _api(responses=None, default=None, project_id="example-project-id", token="example-token"):
    transport = support.RecordingTransport(responses=responses, default_response=default)
    creds = support.Credentials(
        access_key=support.PLACEHOLDER_ACCESS_KEY,
        secret_key=support.PLACEHOLDER_SECRET_KEY,
        security_token=token,
        project_id=project_id,
    )
    return CloudApi(credentials=creds, region="cn-north-4", transport=transport), transport


class StartBuildTest(unittest.TestCase):
    def test_request_shape(self):
        api, transport = _api(default=(200, {}, {"actual_build_number": 42}))
        self.assertEqual("42", api.start_build("job-1", {"PIPELINE_MODE": "plan"}))
        request = transport.last()
        self.assertEqual("POST", request["method"])
        self.assertEqual(
            "https://cloudbuild-ext.cn-north-4.myhuaweicloud.com/v3/jobs/build", request["url"]
        )
        self.assertEqual(
            {"job_id": "job-1", "parameter": [{"name": "PIPELINE_MODE", "value": "plan"}]},
            json.loads(request["body"]),
        )
        self.assertEqual(TIMEOUT_START_BUILD, request["timeout"])
        self.assertEqual(0, request["retries"])
        self.assertEqual("application/json", request["headers"]["Content-Type"])

    def test_build_number_may_be_nested(self):
        api, _ = _api(default=(200, {}, {"result": {"actual_build_number": 7}}))
        self.assertEqual("7", api.start_build("job-1"))

    def test_missing_build_number_is_an_error(self):
        api, _ = _api(default=(200, {}, {"code": "ok"}))
        with self.assertRaises(ServiceError):
            api.start_build("job-1")

    def test_parameters_are_optional(self):
        api, transport = _api(default=(200, {}, {"actual_build_number": 1}))
        api.start_build("job-1")
        self.assertNotIn("parameter", json.loads(transport.last()["body"]))


class DescribeBuildTest(unittest.TestCase):
    def test_path_and_fields(self):
        api, transport = _api(
            default=(
                200,
                {},
                {
                    "status": "running",
                    "result": {
                        "record_status": "Failed",
                        "finish_time": 1767312000000,
                        "err_msg": "exit code 1",
                        "duration": 32,
                    },
                },
            )
        )
        build = api.describe_build("job-1", "42")
        request = transport.last()
        self.assertEqual(
            "https://cloudbuild-ext.cn-north-4.myhuaweicloud.com/v4/jobs/job-1/42/record-info",
            request["url"],
        )
        self.assertEqual("GET", request["method"])
        self.assertEqual(TIMEOUT_QUERY_BUILD, request["timeout"])
        self.assertEqual(RETRY_QUERY_BUILD, request["retries"])
        self.assertEqual("Failed", build["status"])
        self.assertEqual("exit code 1", build["err_msg"])
        self.assertEqual(32, build["duration"])
        self.assertEqual("running", build["raw_status"])

    def test_terminal_predicate(self):
        self.assertTrue(CloudApi.is_terminal({"status": "success"}))
        self.assertTrue(CloudApi.is_terminal({"status": "FAILED"}))
        self.assertTrue(CloudApi.is_terminal({"status": "canceled"}))
        self.assertTrue(CloudApi.is_terminal({"status": "", "finish_time": 1}))
        self.assertTrue(CloudApi.is_terminal({"status": "某种未识别的终态", "finish_time": 1}))
        self.assertFalse(CloudApi.is_terminal({"status": "running"}))
        self.assertFalse(CloudApi.is_terminal({"status": "unknown-status"}))

    def test_success_predicate(self):
        self.assertTrue(CloudApi.is_success({"status": "Success"}))
        self.assertTrue(CloudApi.is_success({"status": "successful"}))
        self.assertFalse(CloudApi.is_success({"status": "successed", "finish_time": 1}) is False)
        self.assertFalse(CloudApi.is_success({"status": "failed"}))


class InvocationTest(unittest.TestCase):
    def test_sync_invocation_uses_urn_and_version_header(self):
        api, transport = _api(default=(200, {"X-Cff-Request-Id": "cff-1"}, {"severity": "low"}))
        payload = {"plan_key": "plans/20260101T000000Z/drift_result.json"}
        self.assertEqual({"severity": "low"}, api.invoke_function_sync(CLASSIFIER_URN, payload))
        request = transport.last()
        self.assertIn("/v2/example-project-id/fgs/functions/", request["url"])
        self.assertIn("urn%3Afss%3Acn-north-4", request["url"])
        self.assertTrue(request["url"].endswith("/invocations"))
        self.assertEqual("v1", request["headers"]["X-CFF-Request-Version"])
        self.assertEqual(TIMEOUT_INVOKE_FUNCTION, request["timeout"])
        self.assertEqual(RETRY_INVOKE_FUNCTION, request["retries"])
        self.assertEqual(payload, json.loads(request["body"]))

    def test_qualifier_goes_to_query(self):
        api, transport = _api(default=(200, {}, {}))
        api.invoke_function_sync(CLASSIFIER_URN, {}, qualifier="prod")
        self.assertTrue(transport.last()["url"].endswith("?qualifier=prod"))

    def test_empty_payload_becomes_object(self):
        api, transport = _api(default=(200, {}, {}))
        api.invoke_function_sync(CLASSIFIER_URN, None)
        self.assertEqual("{}", transport.last()["body"])


class PublishTest(unittest.TestCase):
    def test_topic_publish_shape(self):
        api, transport = _api(default=(200, {}, {"message_id": "m-1"}))
        api.publish_message(TOPIC_URN, "subject", "正文")
        request = transport.last()
        self.assertTrue(request["url"].startswith("https://smn.cn-north-4.myhuaweicloud.com/v2/example-project-id/notifications/topics/"))
        self.assertTrue(request["url"].endswith("/publish"))
        body = json.loads(request["body"])
        self.assertEqual({"subject": "subject", "message": "正文"}, body)
        self.assertEqual(TIMEOUT_PUBLISH, request["timeout"])
        self.assertEqual(RETRY_PUBLISH, request["retries"])


class MetricTest(unittest.TestCase):
    def test_metric_body_shape(self):
        api, transport = _api(default=(200, {}, {}))
        api.report_metric(
            namespace="drift_detection.custom",
            metric_name="DriftDetected",
            value=1,
            dimensions={"Pipeline": "drift-detection"},
        )
        request = transport.last()
        self.assertEqual(
            "https://ces.cn-north-4.myhuaweicloud.com/V1.0/example-project-id/metric-data",
            request["url"],
        )
        body = json.loads(request["body"])
        self.assertEqual("drift_detection.custom", body["metric"]["namespace"])
        self.assertEqual("DriftDetected", body["metric"]["metric_name"])
        self.assertEqual([{"name": "Pipeline", "value": "drift-detection"}], body["metric"]["dimensions"])
        self.assertEqual(1.0, body["value"])
        self.assertEqual("Count", body["unit"])
        self.assertNotIn("ttl", body)
        self.assertEqual(TIMEOUT_METRIC, request["timeout"])
        self.assertEqual(RETRY_METRIC, request["retries"])

    def test_collect_time_is_current(self):
        import time

        api, transport = _api(default=(200, {}, {}))
        api.report_metric("drift_detection.custom", "NoDrift", 1, {"Pipeline": "drift-detection"})
        collect_time = json.loads(transport.last()["body"])["collect_time"] / 1000.0
        now = time.time()
        # 云监控只接受「当前时间前三天到后十分钟」内的数据点
        self.assertLess(abs(collect_time - now), 60)

    def test_ttl_is_optional(self):
        api, transport = _api(default=(200, {}, {}))
        api.report_metric("drift_detection.custom", "NoDrift", 1, {"Pipeline": "x"}, ttl_seconds=600)
        self.assertEqual(600, json.loads(transport.last()["body"])["ttl"])

    def test_hyphenated_namespace_is_reachable_but_documented(self):
        """命名空间由配置提供，代码不擅自改写；含连字符时由服务端拒收。"""
        api, transport = _api(default=(200, {}, {}))
        api.report_metric("drift-detection.custom", "NoDrift", 1, {"Pipeline": "x"})
        self.assertEqual("drift-detection.custom", json.loads(transport.last()["body"])["metric"]["namespace"])


class SigningTest(unittest.TestCase):
    def test_all_requests_carry_authorization(self):
        api, transport = _api(default=(200, {}, {"actual_build_number": 1}))
        api.start_build("job-1")
        headers = transport.last()["headers"]
        self.assertTrue(headers["Authorization"].startswith("SDK-HMAC-SHA256 Access="))
        self.assertEqual(support.PLACEHOLDER_ACCESS_KEY, api.credentials.access_key)
        self.assertIn("x-security-token", headers["Authorization"].split("SignedHeaders=")[1].split(",")[0].split(";"))

    def test_project_id_is_required_for_project_scoped_paths(self):
        api, _ = _api(project_id=None)
        for call in (
            lambda: api.report_metric("ns.item", "m", 1, {"d": "v"}),
            lambda: api.publish_message(TOPIC_URN, "s", "m"),
            lambda: api.invoke_function_sync(CLASSIFIER_URN, {}),
        ):
            with self.assertRaises(ConfigError):
                call()

    def test_region_is_required(self):
        with self.assertRaises(ConfigError):
            CloudApi(credentials=support.Credentials("a", "b"), region="")

    def test_error_response_carries_status_and_request_id(self):
        api, _ = _api(default=(403, {"X-Request-Id": "req-403"}, {"error_msg": "forbidden"}))
        with self.assertRaises(ServiceError) as caught:
            api.start_build("job-1")
        self.assertEqual(403, caught.exception.status_code)
        self.assertEqual("req-403", caught.exception.request_id)


class TransportRetryTest(unittest.TestCase):
    """传输层的退避与重试口径。"""

    def setUp(self):
        from common.httpclient import HttpTransport

        self.transport = HttpTransport()
        self.sleeps = []
        self.transport._sleep = lambda base, attempt: self.sleeps.append((base, attempt))

    def _scripted_send(self, statuses):
        state = {"index": 0}

        def fake_send(method, url, headers, body, timeout):
            status = statuses[min(state["index"], len(statuses) - 1)]
            state["index"] += 1
            return support.Response(status, {}, {"status": status})

        self.transport._send = fake_send
        return state

    def test_retriable_status_is_retried(self):
        state = self._scripted_send([500, 200])
        response = self.transport.request("GET", "https://x/y", retries=2)
        self.assertEqual(200, response.status_code)
        self.assertEqual(2, state["index"])
        self.assertEqual([(1.0, 1)], self.sleeps)

    def test_client_error_is_not_retried(self):
        state = self._scripted_send([400])
        with self.assertRaises(ServiceError):
            self.transport.request("GET", "https://x/y", retries=3)
        self.assertEqual(1, state["index"])
        self.assertEqual([], self.sleeps)

    def test_transport_failure_is_retried_then_raised(self):
        from common.errors import TransportError

        state = self._scripted_send([200])

        def failing_send(method, url, headers, body, timeout):
            state["index"] += 1
            raise TransportError("连接超时", url=url)

        self.transport._send = failing_send
        with self.assertRaises(TransportError):
            self.transport.request("GET", "https://x/y", retries=2)
        self.assertEqual(3, state["index"])
        # 传给退避的是「基数 + 第几次」，逐次翻倍与 8 秒封顶由退避函数自己负责
        # （取值序列见 test_backoff_grows_and_is_capped）
        self.assertEqual([(1.0, 1), (1.0, 2)], self.sleeps)

    def test_zero_retry_means_single_attempt(self):
        state = self._scripted_send([200])
        self.transport.request("POST", "https://x/y", retries=RETRY_START_BUILD)
        self.assertEqual(1, state["index"])

    def test_backoff_grows_and_is_capped(self):
        import time as time_module
        from unittest import mock

        from common.httpclient import HttpTransport

        transport = HttpTransport()
        with mock.patch.object(time_module, "sleep") as sleeper:
            for attempt in (1, 2, 3, 5, 9):
                transport._sleep(1.0, attempt)
        self.assertEqual([1.0, 2.0, 4.0, 8.0, 8.0], [call.args[0] for call in sleeper.call_args_list])

    def test_default_timeout_applies_when_not_given(self):
        from common.httpclient import DEFAULT_TIMEOUT_SECONDS

        captured = {}

        def fake_send(method, url, headers, body, timeout):
            captured["timeout"] = timeout
            return support.Response(200, {}, {})

        self.transport._send = fake_send
        self.transport.request("GET", "https://x/y")
        self.assertEqual(DEFAULT_TIMEOUT_SECONDS, captured["timeout"])


if __name__ == "__main__":
    unittest.main()
