"""函数运行期异常类型。

统一三类失败，便于函数入口做「失败上报」分支：

- :class:`ConfigError`   配置缺失或非法，重试无意义，直接终止并告警；
- :class:`TransportError` 请求未送达或对端无响应，是否可重试由幂等性决定；
- :class:`ServiceError`  对端已响应但返回错误码，附带请求编号便于工单定位；
- :class:`PipelineError` 业务链路失败（构建未成功、无巡检结果等）。
"""


class PipelineRuntimeError(RuntimeError):
    """所有函数自定义异常的基类，携带可安全写入日志的上下文。"""

    def __init__(self, message, **context):
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self):
        payload = {"error": type(self).__name__, "message": self.message}
        payload.update(self.context)
        return payload

    def __str__(self):
        parts = ["%s: %s" % (type(self).__name__, self.message)]
        for key, value in sorted(self.context.items()):
            parts.append("%s=%s" % (key, value))
        return " | ".join(parts)


class ConfigError(PipelineRuntimeError):
    """运行期配置缺失或非法。"""


class TransportError(PipelineRuntimeError):
    """请求超时、连接失败或对端无响应。"""

    def __init__(self, message, url=None, attempt=None, **context):
        super().__init__(message, url=url, attempt=attempt, **context)
        self.url = url
        self.attempt = attempt


class ServiceError(PipelineRuntimeError):
    """云服务返回了非 2xx 响应。"""

    def __init__(self, message, status_code=None, request_id=None, body=None, **context):
        super().__init__(
            message,
            status_code=status_code,
            request_id=request_id,
            **context
        )
        self.status_code = status_code
        self.request_id = request_id
        self.body = body

    def to_dict(self):
        payload = super().to_dict()
        payload["status_code"] = self.status_code
        payload["request_id"] = self.request_id
        # 带上响应体摘要：云服务拒绝时真正的失败原因（错误码 / 字段名）只在响应体里，
        # 不暴露它就只能看到一个 4xx 而无从定位。
        if self.body:
            text = (
                self.body.decode("utf-8", "replace")
                if isinstance(self.body, bytes)
                else str(self.body)
            )
            payload["body"] = text[:800]
        return payload


class PipelineError(PipelineRuntimeError):
    """巡检链路失败：构建未成功、缺少巡检结果、无法取得分级结论等。"""


class BuildFailedError(PipelineError):
    """构建任务进入失败终态。"""

    def __init__(self, message, job_id=None, build_no=None, status=None, err_msg=None, **context):
        super().__init__(message, job_id=job_id, build_no=build_no, status=status, **context)
        self.job_id = job_id
        self.build_no = build_no
        self.status = status
        self.err_msg = err_msg
