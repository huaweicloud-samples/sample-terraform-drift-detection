# -*- coding: utf-8 -*-
"""本地校验用的公共工具：路径装配、假运行上下文与假服务端。

这些替身只服务于单元测试，不接触任何云端环境：

- :class:`FakeContext`  模拟函数运行上下文（用户数据 + 委托临时凭据）
- :class:`FakeApi`      按脚本回放构建状态、函数调用、通知与指标上报，并记录调用序列
- :class:`FakeStore`    用内存字典模拟巡检报告桶的读取
- :func:`runtime_for`   直接装配 :class:`common.runtime.Runtime`，跳过真实凭据获取
"""

import json
import os
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
FUNCTIONS_DIR = TESTS_DIR.parent
REPO_ROOT = FUNCTIONS_DIR.parent.parent

if str(FUNCTIONS_DIR) not in sys.path:
    sys.path.insert(0, str(FUNCTIONS_DIR))

from common import classification  # noqa: E402
from common import plan_parser  # noqa: E402
from common.cloud_api import CloudApi  # noqa: E402
from common.config import Config  # noqa: E402
from common.credentials import Credentials  # noqa: E402
from common.errors import ServiceError, TransportError  # noqa: E402
from common.httpclient import _REQUEST_ID_HEADERS  # noqa: E402  请求编号头与传输层保持一致
from common.runtime import Runtime, get_logger  # noqa: E402

FIXTURES_DIR = TESTS_DIR / "fixtures"

# 与 infra/locals.tf 声明的用户数据键名保持一致；值全部是占位符
ORCHESTRATOR_USER_DATA = {
    "PLAN_JOB_ID": "job-plan-0001",
    "APPLY_JOB_ID": "job-apply-0002",
    "CLASSIFIER_URN": "urn:fss:cn-north-4:project-id-example:function:default:tf-drift-classifier:latest",
    "REPORTS_BUCKET": "tf-drift-reports-example",
    "PLAN_KEY_PREFIX": "plans",
    "OBS_ENDPOINT": "https://obs.cn-north-4.myhuaweicloud.com",
    "SMN_TOPIC_URN": "urn:smn:cn-north-4:project-id-example:tf-drift-alerts",
    "CES_NAMESPACE": "drift_detection.custom",
    "CES_DIMENSION": "Pipeline",
    "CES_DIMENSION_VALUE": "drift-detection",
    "HW_REGION": "cn-north-4",
    "WAIT_SECONDS": "30",
    "POLL_INTERVAL": "1",
    "APPROVE_COMMAND": "terraform apply -auto-approve",
}

CLASSIFIER_USER_DATA = {
    "REPORTS_BUCKET": "tf-drift-reports-example",
    "PLAN_KEY_PREFIX": "plans",
    "OBS_ENDPOINT": "https://obs.cn-north-4.myhuaweicloud.com",
    "CES_NAMESPACE": "drift_detection.custom",
    "CES_DIMENSION": "Pipeline",
    "CES_DIMENSION_VALUE": "drift-detection",
    "HW_REGION": "cn-north-4",
    "HW_PROJECT_ID": "project-id-example",
}

PLACEHOLDER_ACCESS_KEY = "AK-example-placeholder"
PLACEHOLDER_SECRET_KEY = "SK-example-placeholder"
PLACEHOLDER_SECURITY_TOKEN = "TOKEN-example-placeholder"


def fixture_text(name):
    """读取测试样本原文。"""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def fixture_json(name):
    """读取测试样本并解析为 Python 对象。"""
    return json.loads(fixture_text(name))


def load_insight(name):
    """把测试样本解析成归一化视图。"""
    return plan_parser.load(fixture_text(name))


def verdict_of(name):
    """对测试样本直接做分级，返回结论。"""
    return classification.classify(load_insight(name).actionable_changes())


class FakeContext(object):
    """函数运行上下文替身。

    :param user_data: 用户数据；``as_json_string=True`` 时按序列化字符串返回，
        ``wrapped=True`` 时再套一层 ``{"data": "..."}``
    """

    def __init__(self, user_data=None, as_json_string=False, wrapped=False, request_id="req-local-test"):
        self._user_data = dict(user_data or {})
        self._as_json_string = as_json_string
        self._wrapped = wrapped
        self._request_id = request_id

    def getUserData(self):
        if not self._as_json_string:
            return self._user_data
        text = json.dumps(self._user_data, ensure_ascii=False)
        if self._wrapped:
            return json.dumps({"data": text}, ensure_ascii=False)
        return text

    def getSecurityAccessKey(self):
        return PLACEHOLDER_ACCESS_KEY

    def getSecuritySecretKey(self):
        return PLACEHOLDER_SECRET_KEY

    def getSecurityToken(self):
        return PLACEHOLDER_SECURITY_TOKEN

    def getProjectId(self):
        return self._user_data.get("HW_PROJECT_ID")

    def getRequestId(self):
        return self._request_id

    def user_data(self):
        return self._user_data


class RecordingTransport(object):
    """HTTP 传输替身：记录签名后的请求，返回预置响应。

    用于校验接口路径、请求体与超时/重试参数，不产生任何真实网络访问。
    非 2xx 的处理口径与传输层一致：直接抛出带状态码与请求编号的 ``ServiceError``，
    不把错误响应体当成业务数据交给上层解析。
    """

    def __init__(self, responses=None, default_response=None, raise_on=None):
        self.requests = []
        self._responses = list(responses or [])
        self._default = default_response or (200, {}, {"ok": True})
        self._raise_on = list(raise_on or [])

    def request(self, method, url, headers=None, body=None, timeout=None, retries=0, **kwargs):
        record = {
            "method": method,
            "url": url,
            "headers": dict(headers or {}),
            "body": body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body,
            "timeout": timeout,
            "retries": retries,
            "kwargs": kwargs,
        }
        self.requests.append(record)
        if self._raise_on:
            pending = self._raise_on.pop(0)
            if isinstance(pending, Exception):
                raise pending
            status, headers, payload = pending
            return _as_response(_Response(status, headers, payload, record), method, url)
        if self._responses:
            status, headers, payload = self._responses.pop(0)
        else:
            status, headers, payload = self._default
        return _as_response(_Response(status, headers, payload, record), method, url)

    def last(self):
        return self.requests[-1]


def _as_response(response, method, url):
    """2xx 原样返回，其余按传输层口径抛出异常。"""
    if response.ok():
        return response
    raise ServiceError(
        "云服务调用失败",
        status_code=response.status_code,
        request_id=response.request_id(),
        body=response.text[:600],
        method=method,
        url=url,
    )


class _Response(object):
    def __init__(self, status_code, headers, payload, record=None):
        self.status_code = status_code
        self.headers = dict(headers or {})
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.body = text.encode("utf-8")

    @property
    def text(self):
        return self.body.decode("utf-8")

    def json(self):
        if not self.body:
            return None
        return json.loads(self.text)

    def request_id(self):
        lowered = {str(key).lower(): value for key, value in self.headers.items()}
        for name in _REQUEST_ID_HEADERS:
            if lowered.get(name):
                return lowered[name]
        return lowered.get("x-sdk-date") or "req-fake"

    def ok(self):
        return 200 <= self.status_code < 300


# 替身响应的公开别名，用例里按名字读更易读
Response = _Response

class FakeApi(object):
    """云服务封装替身，行为与 :class:`common.cloud_api.CloudApi` 的判定口径一致。

    :param build_plan: 每次查询构建状态返回的记录；最后一条会重复返回
    :param invoke_result: 同步调用分级函数的返回值
    """

    def __init__(
        self,
        build_plan=None,
        invoke_result=None,
        metric_error=None,
        publish_error=None,
        start_build_error=None,
    ):
        self.calls = []
        self.build_plan = list(build_plan or [{"status": "success"}])
        self._build_index = 0
        self.invoke_result = invoke_result
        self.metric_error = metric_error
        self.publish_error = publish_error
        self.start_build_error = start_build_error
        self.metrics = []
        self.published = []

    # -- 构建 ----------------------------------------------------------
    def start_build(self, job_id, parameters=None):
        self.calls.append(("start_build", job_id, dict(parameters or {})))
        if self.start_build_error is not None:
            raise self.start_build_error
        return str(1000 + len([c for c in self.calls if c[0] == "start_build"]))

    def describe_build(self, job_id, build_no):
        index = min(self._build_index, len(self.build_plan) - 1)
        self._build_index += 1
        record = dict(self.build_plan[index])
        record.setdefault("job_id", job_id)
        record.setdefault("build_no", str(build_no))
        self.calls.append(("describe_build", job_id, str(build_no)))
        return record

    @staticmethod
    def is_terminal(build):
        return CloudApi.is_terminal(build)

    @staticmethod
    def is_success(build):
        return CloudApi.is_success(build)

    # -- 函数调用 ------------------------------------------------------
    def invoke_function_sync(self, function_urn, payload, qualifier=None):
        self.calls.append(("invoke_function_sync", function_urn, dict(payload or {})))
        if isinstance(self.invoke_result, Exception):
            raise self.invoke_result
        if self.invoke_result is not None:
            return self.invoke_result
        # 缺省按事件里的巡检结果做一次真实分级，保证链路两端能联起来
        return _inline_classify(payload)

    # -- 通知 ----------------------------------------------------------
    def publish_message(self, topic_urn, subject, message):
        self.published.append({"topic_urn": topic_urn, "subject": subject, "message": message})
        self.calls.append(("publish_message", topic_urn, subject))
        if self.publish_error is not None:
            raise self.publish_error
        return {"message_id": "msg-fake-1"}

    # -- 指标 ----------------------------------------------------------
    def report_metric(self, namespace, metric_name, value, dimensions, unit="Count", ttl_seconds=None):
        self.metrics.append(
            {
                "namespace": namespace,
                "metric_name": metric_name,
                "value": value,
                "dimensions": dict(dimensions),
                "unit": unit,
            }
        )
        self.calls.append(("report_metric", namespace, metric_name, value))
        if self.metric_error is not None:
            raise self.metric_error
        return {}

    # -- 便于断言的取数 ------------------------------------------------
    def names(self):
        return [call[0] for call in self.calls]

    def count(self, name):
        return self.names().count(name)


class FakeStore(object):
    """巡检报告桶读取替身。"""

    def __init__(self, objects=None, keys=None):
        self.objects = dict(objects or {})
        self.keys = list(keys or sorted(self.objects))
        self.list_attempts = 0
        self.missing_key_error = None

    def list_keys(self, prefix):
        self.list_attempts += 1
        return [key for key in self.keys if key.startswith(prefix)]

    def latest_result_key(self, prefix, suffix="drift_result.json", attempts=1, delay_seconds=0):
        candidates = sorted(
            [key for key in self.list_keys(prefix) if key.endswith(suffix)], reverse=True
        )
        return candidates[0] if candidates else None

    def download_json(self, key):
        if key not in self.objects:
            raise ServiceError("对象不存在", status_code=404, key=key)
        return self.objects[key]


def _inline_classify(payload):
    """在替身里复现分级函数的返回结构，用于编排链路的联调断言。"""
    plan = payload.get("plan")
    if plan is None:
        plan = {"drift_detected": False, "resource_changes": []}
    insight = plan_parser.load(plan)
    verdict = classification.classify(insight.actionable_changes())
    verdict["plan_key"] = payload.get("plan_key")
    return verdict


def runtime_for(
    role,
    user_data=None,
    api=None,
    store=None,
    event=None,
    environ=None,
    context=None,
    credentials=None,
):
    """装配一个不触碰云端的 :class:`Runtime`。

    凭据用占位符注入，配置来源与线上一致（用户数据 + 环境变量）。
    """
    declared = ORCHESTRATOR_USER_DATA if role == "orchestrator" else CLASSIFIER_USER_DATA
    values = dict(declared)
    values.update(user_data or {})
    env = {"PATH": os.environ.get("PATH", "")}
    env.update(environ or {})

    config = Config(values=values, role=role, overrides=_overrides_for(event))
    config.validate()
    creds = credentials or Credentials(
        access_key=PLACEHOLDER_ACCESS_KEY,
        secret_key=PLACEHOLDER_SECRET_KEY,
        security_token=PLACEHOLDER_SECURITY_TOKEN,
        project_id=values.get("HW_PROJECT_ID") or "project-id-example",
    )
    return Runtime(
        config=config,
        creds=creds,
        api=api or FakeApi(),
        store=store if store is not None else FakeStore(),
        logger=get_logger("local-test"),
    )


def _overrides_for(event):
    """事件里允许覆盖的配置项，口径与 :mod:`common.config` 保持一致。"""
    if not isinstance(event, dict):
        return {}
    allowed = {
        "plan_job_id": "PLAN_JOB_ID",
        "apply_job_id": "APPLY_JOB_ID",
        "classifier_urn": "CLASSIFIER_URN",
        "reports_bucket": "REPORTS_BUCKET",
        "plan_key_prefix": "PLAN_KEY_PREFIX",
        "smn_topic_urn": "SMN_TOPIC_URN",
        "wait_seconds": "WAIT_SECONDS",
        "poll_interval": "POLL_INTERVAL",
        "region": "HW_REGION",
    }
    overrides = {}
    for event_key, config_key in allowed.items():
        if event.get(event_key) not in (None, ""):
            overrides[config_key] = str(event[event_key])
    return overrides


class QuietAsserts(object):
    """把异常类型断言收敛成一处，便于在多个用例里复用。"""

    @staticmethod
    def is_transport_or_service(error):
        return isinstance(error, (TransportError, ServiceError))


__all__ = [
    "CLASSIFIER_USER_DATA",
    "FakeApi",
    "FakeContext",
    "FakeStore",
    "ORCHESTRATOR_USER_DATA",
    "PLACEHOLDER_ACCESS_KEY",
    "PLACEHOLDER_SECRET_KEY",
    "PLACEHOLDER_SECURITY_TOKEN",
    "RecordingTransport",
    "REPO_ROOT",
    "fixture_json",
    "fixture_text",
    "load_insight",
    "runtime_for",
    "verdict_of",
]
