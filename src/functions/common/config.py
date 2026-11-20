"""运行期配置读取。

配置来源按优先级合并，避免「同一条配置在两个地方各写一份」：

1. 本次调用的事件字段（由调用方显式覆盖，仅用于演练与重试）；
2. 函数用户数据（部署时由基础设施声明注入，即 ``user_data``）；
3. 进程环境变量。

本模块只做取值与类型转换，不做业务判断；缺失必填项时抛
:class:`common.errors.ConfigError`，并在消息里列出缺失的键名。

键名与基础设施声明完全一致，代码侧不新造键名：
``PLAN_JOB_ID`` / ``APPLY_JOB_ID`` / ``CLASSIFIER_URN`` / ``REPORTS_BUCKET`` /
``PLAN_KEY_PREFIX`` / ``OBS_ENDPOINT`` / ``SMN_TOPIC_URN`` / ``CES_NAMESPACE`` /
``CES_DIMENSION`` / ``CES_DIMENSION_VALUE`` / ``HW_REGION`` / ``HW_PROJECT_ID`` /
``WAIT_SECONDS`` / ``POLL_INTERVAL`` / ``APPROVE_COMMAND``。
"""

import json

from common.errors import ConfigError

# 必填项按函数角色分组，入口在调用前一次性校验，避免链路中途才失败
REQUIRED_KEYS = {
    "orchestrator": (
        "PLAN_JOB_ID",
        "APPLY_JOB_ID",
        "CLASSIFIER_URN",
        "REPORTS_BUCKET",
        "PLAN_KEY_PREFIX",
        "OBS_ENDPOINT",
        "SMN_TOPIC_URN",
        "CES_NAMESPACE",
        "CES_DIMENSION",
        "CES_DIMENSION_VALUE",
        "HW_REGION",
        "WAIT_SECONDS",
        "POLL_INTERVAL",
        "APPROVE_COMMAND",
    ),
    "classifier": (
        "REPORTS_BUCKET",
        "PLAN_KEY_PREFIX",
        "OBS_ENDPOINT",
        "CES_NAMESPACE",
        "CES_DIMENSION",
        "CES_DIMENSION_VALUE",
        "HW_REGION",
        "HW_PROJECT_ID",
    ),
}


class Config(object):
    """合并后的只读配置视图。"""

    def __init__(self, values=None, role=None, overrides=None):
        self._values = dict(values or {})
        self._overrides = dict(overrides or {})
        self.role = role

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, context=None, role=None, event=None, environ=None):
        """按「环境变量 → 用户数据 → 事件字段」的顺序合并配置。

        :param context: 函数运行上下文，提供 ``getUserData()``
        :param role: ``orchestrator`` 或 ``classifier``，用于必填项校验
        :param event: 本次调用事件，其中的同名字段可覆盖静态配置
        :param environ: 环境变量映射，默认读进程环境（单测可注入假环境）
        """
        import os

        values = {}
        values.update(dict(environ if environ is not None else os.environ))

        user_data = _read_user_data(context)
        values.update(user_data)

        overrides = _pick_overrides(event or {})
        return cls(values=values, role=role, overrides=overrides)

    # ------------------------------------------------------------------
    # 取值
    # ------------------------------------------------------------------
    def get(self, key, default=None):
        if key in self._overrides:
            return self._overrides[key]
        value = self._values.get(key)
        if value is None or value == "":
            return default
        return value

    def require(self, key):
        value = self.get(key)
        if value is None:
            raise ConfigError("缺少必需的运行期配置项", key=key, role=self.role)
        return value

    def int_of(self, key, default=None):
        raw = self.get(key, default)
        if raw is None:
            return None
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            raise ConfigError("配置项不是整数", key=key, value=raw)

    def float_of(self, key, default=None):
        raw = self.get(key, default)
        if raw is None:
            return None
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            raise ConfigError("配置项不是数值", key=key, value=raw)

    def snapshot(self):
        """返回可安全写日志的配置视图，密钥类键名一律脱敏。"""
        merged = dict(self._values)
        merged.update(self._overrides)
        safe = {}
        for key, value in merged.items():
            if _looks_sensitive(key):
                safe[key] = "<masked>"
            else:
                safe[key] = value
        return safe

    def validate(self):
        """校验当前角色的必填项，一次性报出全部缺失键。"""
        keys = REQUIRED_KEYS.get(self.role or "", ())
        missing = [key for key in keys if self.get(key) in (None, "")]
        if missing:
            raise ConfigError("运行期配置不完整", role=self.role, missing=missing)
        return self


def _read_user_data(context):
    if context is None:
        return {}
    raw = None
    getter = getattr(context, "getUserData", None)
    if callable(getter):
        try:
            raw = getter()
        except Exception:  # 运行上下文形态差异不应阻断巡检
            raw = None
    if raw is None:
        raw = getattr(context, "user_data", None)
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        # 用户数据可能带一层 {"data": "..."} 包装，两种写法都兼容
        try:
            parsed = json.loads(text)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            inner = parsed.get("data")
            if isinstance(inner, str):
                try:
                    parsed = json.loads(inner)
                except ValueError:
                    parsed = {}
            return parsed if isinstance(parsed, dict) else {}
    return {}


_EVENT_OVERRIDE_KEYS = {
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


def _pick_overrides(event):
    """从事件里挑出允许覆盖的配置项，其余事件字段由业务代码单独读取。"""
    overrides = {}
    for event_key, config_key in _EVENT_OVERRIDE_KEYS.items():
        if event_key in event and event[event_key] not in (None, ""):
            overrides[config_key] = str(event[event_key])
    return overrides


def _looks_sensitive(key):
    upper = str(key).upper()
    return any(token in upper for token in ("SECRET", "ACCESS_KEY", "TOKEN", "PASSWORD"))
