"""带超时与重试上限的 HTTP 传输层。

设计约束：

- **不假设运行环境装了哪个 HTTP 库**：优先用 ``requests``（函数运行环境内置），
  取不到时退回标准库 ``urllib.request``，两条路径的行为与异常类型完全一致。
- **每个调用点必须显式给超时**（秒），默认 10 秒，避免整条链路挂死。
- **重试次数由调用方按幂等性决定**：查询类接口给重试，触发构建这类会改变外部状态的
  接口只允许「明确失败可重试」，网络超时等结果未知的情形不重试（见 :mod:`common.cloud_api`）。
- 非 2xx 一律抛 :class:`common.errors.ServiceError` 并带上响应体与请求编号，
  由函数入口统一转成告警与结构化返回值。
"""

import json
import time

from common.errors import ServiceError, TransportError

DEFAULT_TIMEOUT_SECONDS = 10
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)
_REQUEST_ID_HEADERS = (
    "x-request-id",
    "x-cff-request-id",
    "x-meeting-request-id",
    "resp_id",
)


class HttpResponse(object):
    def __init__(self, status_code, headers, body):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.body = body or b""

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        if not self.body:
            return None
        try:
            return json.loads(self.text)
        except ValueError:
            raise ServiceError(
                "响应体不是合法 JSON",
                status_code=self.status_code,
                request_id=self.request_id(),
                body=_truncate(self.text),
            )

    def request_id(self):
        lowered = {str(k).lower(): v for k, v in self.headers.items()}
        for name in _REQUEST_ID_HEADERS:
            if lowered.get(name):
                return lowered[name]
        return lowered.get("x-sdk-date")

    def ok(self):
        return 200 <= self.status_code < 300


class HttpTransport(object):
    """最小 HTTP 客户端。"""

    def __init__(self, logger=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
        self.logger = logger
        self.timeout_seconds = timeout_seconds
        self._session = None

    # ------------------------------------------------------------------
    def request(
        self,
        method,
        url,
        headers=None,
        body=None,
        timeout=None,
        retries=0,
        retry_backoff_seconds=1.0,
        retry_on_status=_RETRYABLE_STATUS,
    ):
        """发起请求并按重试上限退避重试。

        :param retries: 额外重试次数，0 表示只请求一次
        :param retry_on_status: 命中这些状态码才重试
        :raises TransportError: 请求始终未能得到响应（含重试耗尽）
        :raises ServiceError: 得到响应但状态码非 2xx
        """
        timeout = timeout or self.timeout_seconds
        attempts = max(int(retries), 0) + 1
        last_transport_error = None
        last_response = None

        for attempt in range(1, attempts + 1):
            try:
                response = self._send(method, url, headers, body, timeout)
            except TransportError as error:
                error.attempt = attempt
                last_transport_error = error
                self._log(
                    "warning",
                    "请求未得到响应",
                    method=method,
                    url=url,
                    attempt=attempt,
                    detail=str(error),
                )
                if attempt >= attempts:
                    raise
                self._sleep(retry_backoff_seconds, attempt)
                continue

            if response.ok():
                return response

            retriable = response.status_code in retry_on_status
            self._log(
                "warning" if retriable else "error",
                "服务返回错误响应",
                method=method,
                url=url,
                status_code=response.status_code,
                request_id=response.request_id(),
                attempt=attempt,
            )
            if retriable and attempt < attempts:
                last_response = response
                self._sleep(retry_backoff_seconds, attempt)
                continue
            raise ServiceError(
                "云服务调用失败",
                status_code=response.status_code,
                request_id=response.request_id(),
                body=_truncate(response.text),
                method=method,
                url=url,
            )

        if last_response is not None:  # 理论上不会到这里，保底抛业务异常
            raise ServiceError(
                "云服务调用失败",
                status_code=last_response.status_code,
                request_id=last_response.request_id(),
                body=_truncate(last_response.text),
            )
        raise last_transport_error or TransportError("请求失败", url=url)

    # ------------------------------------------------------------------
    def _send(self, method, url, headers, body, timeout):
        try:
            import requests  # 函数运行环境内置
        except ImportError:
            return self._send_with_urllib(method, url, headers, body, timeout)

        try:
            if self._session is None:
                self._session = requests.Session()
            response = self._session.request(
                method=method.upper(),
                url=url,
                headers=headers,
                data=body,
                timeout=timeout,
            )
        except requests.RequestException as error:
            raise TransportError("请求传输失败", url=url, detail=str(error))
        return HttpResponse(response.status_code, response.headers, response.content)

    def _send_with_urllib(self, method, url, headers, body, timeout):
        from urllib.error import HTTPError, URLError
        from urllib.request import Request, urlopen

        request = Request(url, data=body, method=method.upper())
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urlopen(request, timeout=timeout) as raw:
                return HttpResponse(raw.getcode(), dict(raw.headers.items()), raw.read())
        except HTTPError as error:
            # 错误响应同样要交给上层判定状态码，才能区分「可重试」与「确定失败」
            return HttpResponse(
                error.code,
                dict(error.headers.items()) if error.headers else {},
                error.read() if hasattr(error, "read") else b"",
            )
        except (URLError, OSError, ValueError) as error:
            raise TransportError("请求传输失败", url=url, detail=str(error))

    def _sleep(self, base_seconds, attempt):
        time.sleep(min(base_seconds * (2 ** (attempt - 1)), 8.0))

    def _log(self, level, message, **fields):
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(
            "%s %s", message, json.dumps(fields, default=str, ensure_ascii=False)
        )


def _truncate(text, limit=600):
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "..."
