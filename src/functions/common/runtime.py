"""函数运行时的装配层。

把「配置 + 凭据 + 服务端点 + 报告桶」的组装收在一处，两个函数的入口只做业务流程，
单测则通过替换本模块的构造结果来驱动完整链路，不需要连任何云端环境。

装配顺序：

1. 读运行期配置（事件字段 > 用户数据 > 环境变量）并按角色校验必填项；
2. 取本次调用的临时凭据；项目 ID 优先取配置，缺失时从分级函数 URN 反解；
3. 构造服务端点集合与报告桶读取入口。

日志写标准输出，由日志服务按函数各自的日志流采集。
"""

import json
import logging

from common import config as config_module
from common import credentials as credentials_module
from common.cloud_api import CloudApi
from common.errors import ConfigError
from common.httpclient import HttpTransport
from common.obs_store import ReportStore

LOGGER_NAME = "drift-detection"


def get_logger(event_id=None):
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    if event_id:
        return _ChildLogger(logger, event_id)
    return logger


class _ChildLogger(object):
    """给每条日志加上本次调用编号，便于在日志流里区分不同巡检。"""

    def __init__(self, logger, event_id):
        self._logger = logger
        self._prefix = "request_id=%s" % event_id

    def _emit(self, level, message, *args):
        getattr(self._logger, level)("%s %s", self._prefix, message, *args)

    def info(self, message, *args):
        self._emit("info", message, *args)

    def warning(self, message, *args):
        self._emit("warning", message, *args)

    def error(self, message, *args):
        self._emit("error", message, *args)

    def debug(self, message, *args):
        self._emit("debug", message, *args)


class Runtime(object):
    """一次函数调用所需的全部外部依赖。"""

    def __init__(self, config, creds, api, store, logger):
        self.config = config
        self.credentials = creds
        self.api = api
        self.store = store
        self.logger = logger

    def describe(self):
        payload = {
            "role": self.config.role,
            "region": self.config.get("HW_REGION"),
            "reports_bucket": self.config.get("REPORTS_BUCKET"),
            "plan_key_prefix": self.config.get("PLAN_KEY_PREFIX"),
            "credentials": self.credentials.redacted(),
        }
        return payload


def build(role, event, context=None, environ=None, store_factory=None, api_factory=None):
    """装配 :class:`Runtime`。

    :param role: ``orchestrator`` / ``classifier``
    :param event: 本次调用事件
    :param context: 函数运行上下文
    :param environ: 注入的环境变量映射（单测用）
    :param store_factory: 报告桶构造器，单测替换用
    :param api_factory: 服务端点构造器，单测替换用
    """
    event = event if isinstance(event, dict) else _coerce_event(event)
    event_id = _event_id(context)
    logger = get_logger(event_id)

    cfg = config_module.Config.load(context=context, role=role, event=event, environ=environ)
    cfg.validate()

    project_id_hint = cfg.get("HW_PROJECT_ID") or credentials_module.project_id_from_urn(
        cfg.get("CLASSIFIER_URN")
    )
    creds = credentials_module.resolve(
        context=context, environ=environ, project_id_hint=project_id_hint
    )

    api = (api_factory or _default_api_factory)(cfg, creds, logger)
    store = (store_factory or _default_store_factory)(cfg, creds, logger)
    return Runtime(config=cfg, creds=creds, api=api, store=store, logger=logger)


def _default_api_factory(cfg, creds, logger):
    return CloudApi(credentials=creds, region=cfg.require("HW_REGION"), logger=logger)


def _default_store_factory(cfg, creds, logger):
    return ReportStore(
        endpoint=cfg.require("OBS_ENDPOINT"),
        bucket=cfg.require("REPORTS_BUCKET"),
        credentials=creds,
        logger=logger,
    )


def _coerce_event(event):
    if isinstance(event, (bytes, bytearray)):
        event = bytes(event).decode("utf-8", "replace")
    if isinstance(event, str):
        text = event.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError:
            return {"raw": text}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    if event is None:
        return {}
    return {"raw": event}


def _event_id(context):
    if context is None:
        return None
    for name in ("getRequestId", "get_request_id"):
        getter = getattr(context, name, None)
        if callable(getter):
            try:
                value = getter()
            except Exception:
                continue
            if value:
                return value
    return getattr(context, "request_id", None)


__all__ = ["Runtime", "build", "get_logger", "ConfigError"]
