"""云服务能力封装。

编排函数与分级函数对华为云侧的**全部**调用都收口到这一个类，
每个方法都固定了三件事：超时、重试上限、失败时抛出的异常类型。

调用的接口与用途：

=================================  ==========================================
接口                              用途
=================================  ==========================================
``POST /v3/jobs/build``            触发一次构建（巡检或修复）
``GET  /v4/jobs/{job}/{no}/record-info``  查询一次构建的终态与耗时
``POST /v2/{project}/fgs/functions/{urn}/invocations``  同步调用分级函数
``POST /v2/{project}/notifications/topics/{urn}/publish``  发布告警与审批提醒
``POST /V1.0/{project}/metric-data``  上报一条自定义指标
=================================  ==========================================

域名按区域拼装，服务名固定：构建 ``cloudbuild-ext``、函数 ``functiongraph``、
消息通知 ``smn``、云监控 ``ces``，格式均为 ``https://<服务>.<区域>.myhuaweicloud.com``。

重试口径（写在常量里，便于逐点核对）：

- 查询类（构建状态、指标上报、消息发布）：结果确定失败可换一次重试，上限 2 次；
- 触发构建：**会改变外部状态**，只请求一次。失败即上报，由下一个巡检周期自然补偿；
- 同步调用分级函数：最多重试 1 次，分级逻辑只做「读取 + 上报指标」，
  重复执行最多产生重复数据点，不会重复修改资源。
"""

import json
from urllib.parse import quote

from common import signer
from common.errors import ConfigError, ServiceError
from common.httpclient import HttpTransport

# 各调用的超时（秒）与重试上限
TIMEOUT_START_BUILD = 20
TIMEOUT_QUERY_BUILD = 10
TIMEOUT_INVOKE_FUNCTION = 120
TIMEOUT_PUBLISH = 10
TIMEOUT_METRIC = 10

RETRY_QUERY_BUILD = 2
RETRY_INVOKE_FUNCTION = 1
RETRY_PUBLISH = 2
RETRY_METRIC = 2
RETRY_START_BUILD = 0

_BUILD_SERVICE = "cloudbuild-ext"
_FGS_SERVICE = "functiongraph"
_SMN_SERVICE = "smn"
_CES_SERVICE = "ces"

# 构建终态：命中即结束轮询；其余取值一律视为「仍在执行」，等到超时为止。
TERMINAL_BUILD_STATUSES = {
    "success",
    "successed",
    "successful",
    "failed",
    "fail",
    "error",
    "canceled",
    "cancelled",
    "stopped",
    "abort",
    "aborted",
    "timeout",
}


class CloudApi(object):
    """一次函数调用内使用的服务端点集合。"""

    def __init__(self, credentials, region, logger=None, transport=None):
        if not region:
            raise ConfigError("缺少区域配置", key="HW_REGION")
        self.credentials = credentials
        self.region = region
        self.logger = logger
        self.transport = transport or HttpTransport(logger=logger)

    # ------------------------------------------------------------------
    # 构建：触发与轮询
    # ------------------------------------------------------------------
    def start_build(self, job_id, parameters=None):
        """触发一次构建，返回构建编号。

        只请求一次（``RETRY_START_BUILD``）：编号未知的重复触发会产生两条构建记录，
        因此失败直接上抛，由调用方上报后等下一个巡检周期。
        """
        body = {"job_id": job_id}
        if parameters:
            body["parameter"] = [
                {"name": str(name), "value": str(value)} for name, value in dict(parameters).items()
            ]
        payload = self._call(
            "POST",
            _BUILD_SERVICE,
            "/v3/jobs/build",
            body=body,
            timeout=TIMEOUT_START_BUILD,
            retries=RETRY_START_BUILD,
        )
        build_no = _first_of(payload, "actual_build_number", "build_no", default=None)
        if build_no is None and isinstance(payload.get("result"), dict):
            build_no = _first_of(payload["result"], "actual_build_number", "build_no", default=None)
        if build_no in (None, ""):
            raise ServiceError(
                "构建已触发但未返回构建编号",
                job_id=job_id,
                body=_truncate(payload),
            )
        return str(build_no)

    def describe_build(self, job_id, build_no):
        """查询一次构建的执行记录，返回精简后的状态视图。"""
        path = "/v4/jobs/%s/%s/record-info" % (_encode(job_id), _encode(build_no))
        payload = self._call(
            "GET",
            _BUILD_SERVICE,
            path,
            timeout=TIMEOUT_QUERY_BUILD,
            retries=RETRY_QUERY_BUILD,
        )
        record = payload if isinstance(payload, dict) else {}
        if isinstance(record.get("result"), dict):
            record = record["result"]
        status = _first_of(record, "record_status", "status", default="")
        return {
            "job_id": job_id,
            "build_no": str(build_no),
            "status": str(status or ""),
            "finish_time": record.get("finish_time"),
            "err_msg": record.get("err_msg"),
            "duration": record.get("duration"),
            "raw_status": _first_of(payload, "status", default=None) if isinstance(payload, dict) else None,
        }

    @staticmethod
    def is_terminal(build):
        """判断构建是否已到终态。

        状态取值以服务侧回填为准，未识别的取值按「有结束时间即终态」兜底，
        避免因为新增状态字符串而无限等待。
        """
        status = str(build.get("status") or "").strip().lower()
        if status in TERMINAL_BUILD_STATUSES:
            return True
        return bool(build.get("finish_time"))

    @staticmethod
    def is_success(build):
        return str(build.get("status") or "").strip().lower() in (
            "success",
            "successed",
            "successful",
        )

    # ------------------------------------------------------------------
    # 函数：同步调用分级函数
    # ------------------------------------------------------------------
    def invoke_function_sync(self, function_urn, payload, qualifier=None):
        """同步调用函数并取回返回值。

        ``X-CFF-Request-Version: v1`` 让响应体直接是函数的返回 JSON，
        编排函数据此判断分级结论，不必再回读存储。

        :param function_urn: 目标函数完整 URN
        :param payload: 传给目标函数的事件字典
        :param qualifier: 别名或版本，缺省由服务侧按默认别名执行
        """
        project_id = self._require_project_id()
        path = "/v2/%s/fgs/functions/%s/invocations" % (
            _encode(project_id),
            _encode(function_urn),
        )
        query = [("qualifier", qualifier)] if qualifier else []
        return self._call(
            "POST",
            _FGS_SERVICE,
            path,
            body=payload or {},
            query=query,
            headers={"X-CFF-Request-Version": "v1"},
            timeout=TIMEOUT_INVOKE_FUNCTION,
            retries=RETRY_INVOKE_FUNCTION,
        )

    # ------------------------------------------------------------------
    # 消息通知
    # ------------------------------------------------------------------
    def publish_message(self, topic_urn, subject, message):
        """向告警主题发布一条消息（邮件订阅需已确认，否则不会送达）。"""
        project_id = self._require_project_id()
        path = "/v2/%s/notifications/topics/%s/publish" % (
            _encode(project_id),
            _encode(topic_urn),
        )
        return self._call(
            "POST",
            _SMN_SERVICE,
            path,
            body={"subject": subject, "message": message},
            timeout=TIMEOUT_PUBLISH,
            retries=RETRY_PUBLISH,
        )

    # ------------------------------------------------------------------
    # 云监控
    # ------------------------------------------------------------------
    def report_metric(self, namespace, metric_name, value, dimensions, unit="Count", ttl_seconds=None):
        """上报一条自定义指标。

        ``collect_time`` 用毫秒时间戳，且必须落在「当前时间前三天到后十分钟」内，
        否则服务端拒收；本方法总是取当前时间，不传历史值。
        """
        import time as _time

        project_id = self._require_project_id()
        body = {
            "metric": {
                "namespace": namespace,
                "metric_name": metric_name,
                "dimensions": [
                    {"name": str(name), "value": str(val)} for name, val in dict(dimensions).items()
                ],
            },
            "collect_time": int(_time.time() * 1000),
            "value": float(value),
            "unit": unit,
        }
        if ttl_seconds:
            body["ttl"] = int(ttl_seconds)
        path = "/V1.0/%s/metric-data" % _encode(project_id)
        return self._call(
            "POST",
            _CES_SERVICE,
            path,
            body=body,
            timeout=TIMEOUT_METRIC,
            retries=RETRY_METRIC,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _call(
        self,
        method,
        service,
        path,
        body=None,
        query=None,
        headers=None,
        timeout=10,
        retries=0,
    ):
        encoded_body = None
        if body is not None:
            encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers = {"Accept": "application/json"}
        if encoded_body is not None:
            request_headers["Content-Type"] = "application/json"
        for key, value in (headers or {}).items():
            request_headers[key] = value

        signed = signer.build_headers(
            host=self._host(service),
            path=path,
            method=method,
            query_params=query or [],
            headers=request_headers,
            body=encoded_body,
            credentials=self.credentials,
        )
        headers_out, url = signed
        response = self.transport.request(
            method=method,
            url=url,
            headers=headers_out,
            body=encoded_body,
            timeout=timeout,
            retries=retries,
        )
        payload = response.json()
        _log_response(self.logger, service, path, response, payload)
        return payload

    def _host(self, service):
        return "%s.%s.myhuaweicloud.com" % (service, self.region)

    def _require_project_id(self):
        project_id = getattr(self.credentials, "project_id", None)
        if not project_id:
            raise ConfigError(
                "缺少项目 ID：请确认函数配置里已下发 HW_PROJECT_ID，或调用方传入函数 URN"
            )
        return project_id


def _encode(value):
    """路径参数按 URL 规范整体转义（含 ``:`` 与 ``/``）。"""
    return quote(str(value), safe="")


def _first_of(mapping, *names, **kwargs):
    default = kwargs.get("default")
    for name in names:
        if isinstance(mapping, dict) and mapping.get(name) not in (None, ""):
            return mapping[name]
    return default


def _truncate(payload, limit=400):
    text = json.dumps(payload, default=str, ensure_ascii=False) if payload is not None else ""
    return text if len(text) <= limit else text[:limit] + "..."


def _log_response(logger, service, path, response, payload):
    if logger is None:
        return
    logger.info(
        "service=%s path=%s status=%s request_id=%s body=%s",
        service,
        path,
        response.status_code,
        response.request_id(),
        _truncate(payload),
    )
