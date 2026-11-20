"""巡检报告桶的读取。

只读不写：巡检结果由构建脚本写入，函数侧只负责取回最新一份。

- 客户端用函数运行环境内置的对象存储 SDK，不在代码包里重复打包；
  取不到该模块时抛 :class:`common.errors.ConfigError`，错误信息指向运行时选择。
- 访问身份是委托签发的临时密钥对（含安全令牌）；桶侧已用桶策略放行该委托，
  对象存储数据面只认桶策略，因此权限变更要同时改委托与桶策略。
- 列举对象有秒级一致性延迟，构建刚结束时可能列不到最新对象，
  因此读取带有限次重试（默认 3 次、间隔 5 秒）。
"""

import time

from common.errors import ConfigError, PipelineError, ServiceError, TransportError

DEFAULT_LIST_ATTEMPTS = 3
DEFAULT_LIST_DELAY_SECONDS = 5
MAX_KEYS_PER_LIST = 1000
DRIFT_RESULT_SUFFIX = "drift_result.json"


class ReportStore(object):
    """巡检报告桶的读取入口。"""

    def __init__(self, endpoint, bucket, credentials, logger=None):
        if not endpoint or not bucket:
            raise ConfigError("缺少报告桶配置", keys=["OBS_ENDPOINT", "REPORTS_BUCKET"])
        self.bucket = bucket
        self.logger = logger
        self._client = _build_client(endpoint, credentials, logger)

    # ------------------------------------------------------------------
    def list_keys(self, prefix):
        """列出指定前缀下的对象键。"""
        response = self._client.listObjects(
            self.bucket, prefix=prefix, max_keys=MAX_KEYS_PER_LIST
        )
        _require_ok(response, "列举巡检结果", bucket=self.bucket, prefix=prefix)
        contents = getattr(getattr(response, "body", None), "contents", None) or []
        keys = []
        for item in contents:
            key = getattr(item, "key", None)
            if key:
                keys.append(str(key))
        return keys

    def latest_result_key(
        self,
        prefix,
        suffix=DRIFT_RESULT_SUFFIX,
        attempts=DEFAULT_LIST_ATTEMPTS,
        delay_seconds=DEFAULT_LIST_DELAY_SECONDS,
    ):
        """取最近一次巡检结果的对象键。

        键名形如 ``plans/<UTC 时间戳>/drift_result.json``，时间戳定宽且按字典序递增，
        所以直接按键名排序即为时间序，比依赖返回体里的时间字段更稳定。
        """
        last_error = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                candidates = [key for key in self.list_keys(prefix) if key.endswith(suffix)]
            except (ServiceError, TransportError) as error:
                last_error = error
                candidates = []
            if candidates:
                return sorted(candidates, reverse=True)[0]
            if attempt < attempts:
                if last_error is None and self.logger is not None:
                    self.logger.warning(
                        "巡检结果暂不可见，稍后重试 prefix=%s attempt=%s", prefix, attempt
                    )
                time.sleep(delay_seconds)
        return None

    def download_json(self, key):
        """下载对象并按 JSON 解析。"""
        response = self._client.getObject(self.bucket, key, loadStreamInMemory=True)
        _require_ok(response, "下载巡检结果", bucket=self.bucket, key=key)
        raw = _read_body(response)
        if raw is None:
            raise PipelineError("巡检结果对象为空", bucket=self.bucket, key=key)
        import json

        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as error:
            raise PipelineError(
                "巡检结果不是合法 JSON", bucket=self.bucket, key=key, detail=str(error)
            )


def _build_client(endpoint, credentials, logger):
    try:
        from obs import ObsClient  # 函数运行环境内置
    except ImportError as error:
        raise ConfigError(
            "当前运行时没有对象存储客户端模块，请改用内置该模块的 Python 运行时",
            detail=str(error),
        )

    kwargs = {
        "access_key_id": credentials.access_key,
        "secret_access_key": credentials.secret_key,
        "server": endpoint,
    }
    if getattr(credentials, "security_token", None):
        kwargs["security_token"] = credentials.security_token
    return ObsClient(**kwargs)


def _require_ok(response, action, **context):
    status = getattr(response, "status", None)
    if status is not None and 200 <= int(status) < 300:
        return
    raise ServiceError(
        "%s失败" % action,
        status_code=status,
        body=_error_message(response),
        **context
    )


def _error_message(response):
    body = getattr(response, "body", None)
    for name in ("error_msg", "errorMessage", "code"):
        value = getattr(body, name, None)
        if value:
            return str(value)
    reason = getattr(response, "reason", None)
    return str(reason) if reason else None


def _read_body(response):
    body = getattr(response, "body", None)
    if body is None:
        return None
    data = getattr(body, "buffer", None)
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    stream = getattr(body, "response", None)
    if stream is not None and hasattr(stream, "read"):
        return stream.read()
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    return None
